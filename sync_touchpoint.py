"""Read-only Zoho Sprints MCP -> CSV and optional Google Sheets.

Environment:
  ZOHO_MCP_URL           required; keep private
  GOOGLE_SHEET_ID        optional, enables Google Sheets upload
  GOOGLE_APPLICATION_CREDENTIALS  required for Sheets upload

Authentication is interactive/in-memory for development; not yet suitable for
unattended Cloud Run scheduling.
"""
import asyncio
import csv
import json
import os
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from pydantic import AnyUrl
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata

TEAM_ID = "919475195"
PROJECT_ID = "178933000000009002"
OUTPUT_DIR = Path("export")
CHECKPOINT_DIR = OUTPUT_DIR / "checkpoints"
PAGE_SIZE = 250
HEADERS = {"x-za-ui-version": "v2", "X-convert-response": "true"}

class InMemoryTokenStorage:
    def __init__(self):
        self.tokens = None
        self.client_info = None
    async def get_tokens(self):
        return self.tokens
    async def set_tokens(self, tokens):
        self.tokens = tokens
    async def get_client_info(self):
        return self.client_info
    async def set_client_info(self, client_info):
        self.client_info = client_info

async def open_browser(url):
    print("Opening Zoho sign-in in browser...")
    webbrowser.open(url)

async def handle_callback():
    print("If localhost refuses the callback, paste that URL into the terminal only.")
    callback_url = input("OAuth callback URL: ").strip()
    parsed = urlparse(callback_url)
    if (parsed.scheme, parsed.hostname, parsed.port, parsed.path) != ("http", "localhost", 8765, "/callback"):
        raise ValueError("Unexpected OAuth callback address")
    params = parse_qs(parsed.query)
    if "error" in params:
        raise RuntimeError("OAuth declined")
    if not params.get("code") or not params.get("state"):
        raise ValueError("Missing OAuth code/state")
    return AuthorizationCodeResult(code=params["code"][0], state=params["state"][0], iss=params.get("iss", [None])[0])

def unpack(result):
    # Installed MCP SDK uses snake_case (as confirmed in schema discovery).
    if result.is_error:
        message = " | ".join(str(getattr(part, "text", ""))[:400] for part in result.content)
        raise RuntimeError(f"MCP tool error: {message}")
    if result.structured_content is not None:
        obj = result.structured_content
    else:
        texts = [part.text for part in result.content if part.type == "text"]
        if not texts:
            raise RuntimeError("Empty MCP response")
        try:
            obj = json.loads(texts[0])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON MCP response: {texts[0][:150]}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"Unexpected MCP response type: {type(obj).__name__}")
    if obj.get("status") not in (None, "success"):
        raise RuntimeError(f"Zoho returned status {obj.get('status')!r}: {str(obj)[:250]}")
    if isinstance(obj.get("data"), dict) and obj["data"].get("status") not in (None, "success"):
        raise RuntimeError(f"Zoho returned data status: {obj['data'].get('status')}")
    return obj

async def call(session, name, path=None, query=None):
    args = {}
    if path is not None:
        args = {"headers": HEADERS, "path_variables": path, "query_params": query or {}}
    return unpack(await session.call_tool(name, arguments=args))

def get_collection(response, key):
    data = response.get("data", response)
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected collection payload")
    aliases = {
        "activity": ("activity", "activities", "activityLog", "logs", "history"),
        "items": ("items", "workItems"),
        "sprints": ("sprints",),
    }
    for candidate in aliases.get(key, (key,)):
        if isinstance(data.get(candidate), list):
            return data[candidate], data.get("next")
    # Fallback only when there is exactly one list in payload.
    found = [k for k, v in data.items() if isinstance(v, list)]
    if len(found) == 1:
        return data[found[0]], data.get("next")
    raise RuntimeError(f"Missing expected '{key}' array. Response data keys: {sorted(data.keys())}")

async def pages(session, tool, path, key, extra=None):
    start = 1
    results = []
    for _ in range(1000):
        query = {"index": start, "range": PAGE_SIZE, **(extra or {})}
        response = await call(session, tool, path, query)
        batch, has_next = get_collection(response, key)
        results.extend(batch)
        if has_next is False or has_next == "false":
            break
        if not batch:
            break
        if has_next is None and len(batch) < PAGE_SIZE:
            break
        start += len(batch)
    else:
        raise RuntimeError(f"Pagination limit exceeded for {tool}")
    return results

def scalar(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)

def normalize(rows, preferred=()):
    keys = list(preferred)
    seen = set(keys)
    for row in rows:
        for key in row:
            if key not in seen:
                keys.append(key)
                seen.add(key)
    return keys, [[scalar(row.get(key)) for key in keys] for row in rows]

def write_csv(tab, rows, preferred=()):
    OUTPUT_DIR.mkdir(exist_ok=True)
    headers, values = normalize(rows, preferred)
    if not headers:
        headers = ["no_data"]
    path = OUTPUT_DIR / f"{tab}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
        writer.writerows(values)
    print(f"Saved {path} ({len(rows)} rows)")
    return [headers, *values]

def save_checkpoint(sprint_id, payload):
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    destination = CHECKPOINT_DIR / f"sprint_{sprint_id}.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(destination)

def load_checkpoint(sprint_id):
    destination = CHECKPOINT_DIR / f"sprint_{sprint_id}.json"
    if not destination.exists():
        return None
    try:
        payload = json.loads(destination.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if payload.get("complete") else None

async def sync_all(session):
    project_path = {"teamId": TEAM_ID, "projectId": PROJECT_ID}
    print("Reading all TouchPoint sprints...")
    sprints = await pages(session, "ZohoSprints_GetSprints", project_path, "sprints", {"action": "data", "type": "[1,2,3,4]"})
    if not sprints:
        raise RuntimeError("No sprints found: check the workspace/project ID")
    print(f"Found {len(sprints)} sprints")

    items_by_id = {}
    item_memberships = []
    raw_activities = []
    issues = []
    activity_success = 0
    for i, sprint in enumerate(sprints, 1):
        sprint_id = str(sprint.get("sprintId", ""))
        if not sprint_id:
            issues.append({"stage": "sprint", "id": "", "error": "Missing sprintId"})
            continue
        path = {**project_path, "sprintId": sprint_id}
        print(f"[{i}/{len(sprints)}] {sprint.get('sprintName', sprint_id)}")
        checkpoint = load_checkpoint(sprint_id)
        if checkpoint is not None:
            print(f"  Reusing saved checkpoint ({len(checkpoint['items'])} items)")
            for row in checkpoint["items"]:
                item_id = str(row.get("itemId") or row.get("id") or "")
                if item_id:
                    item_memberships.append({"itemId": item_id, "sprintId": sprint_id, "projectId": PROJECT_ID})
                    items_by_id[(sprint_id, item_id)] = {"sprintId": sprint_id, "projectId": PROJECT_ID, **row}
            raw_activities.extend(checkpoint["activities"])
            activity_success += checkpoint.get("activity_success", 0)
            issues.extend(checkpoint.get("issues", []))
            continue
        sprint_issues = []
        sprint_activities = []
        sprint_activity_success = 0
        try:
            batch = await pages(session, "ZohoSprints_GetItems", path, "items", {"action": "data", "subitem": True})
        except Exception as exc:
            # Do not silently imply the export is complete.
            issues.append({"stage": "items", "id": sprint_id, "error": str(exc)})
            print(f"  Item retrieval FAILED: {str(exc)[:200]}")
            continue
        print(f"  {len(batch)} work items")
        for item in batch:
            item_id = str(item.get("itemId") or item.get("id") or "")
            if not item_id:
                issues.append({"stage": "item", "id": sprint_id, "error": "Cannot identify work-item ID"})
                continue
            # Item rows keep their sprint ID; memberships preserve duplicates if
            # Zoho returns a work item in multiple sprints.
            item_memberships.append({"itemId": item_id, "sprintId": sprint_id, "projectId": PROJECT_ID})
            items_by_id[(sprint_id, item_id)] = {"sprintId": sprint_id, "projectId": PROJECT_ID, **item}
            item_path = {**path, "itemId": item_id}
            try:
                events = await pages(session, "ZohoSprints_GetItemActivity", item_path, "activity", {})
                activity_success += 1
                sprint_activity_success += 1
                for event in events:
                    activity_row = {"sprintId": sprint_id, "itemId": item_id, **event}
                    raw_activities.append(activity_row)
                    sprint_activities.append(activity_row)
            except Exception as exc:
                err = {"stage": "activity", "id": item_id, "error": str(exc)}
                issues.append(err)
                sprint_issues.append(err)
                if len(issues) <= 5:
                    print(f"  Activity retrieval issue for {item_id}: {str(exc)[:140]}")
        # Only cache a fully retrieved sprint; incomplete activity remains retryable.
        if not sprint_issues:
            save_checkpoint(sprint_id, {
                "complete": True, "items": batch,
                "activities": sprint_activities,
                "activity_success": sprint_activity_success,
                "issues": [],
            })
            print("  Checkpoint saved")

    log = [{"sync_utc": datetime.now(timezone.utc).isoformat(),
            "projectId": PROJECT_ID, "sprints": len(sprints), "item_rows": len(items_by_id),
            "activity_rows": len(raw_activities), "activity_fetch_success": activity_success,
            "errors": len(issues), "status": "partial" if issues else "success"}]
    tables = {
        "Sprints": write_csv("Sprints", sprints, ("sprintId", "sprintName", "sprintType", "startDate", "endDate")),
        "Work_Items": write_csv("Work_Items", list(items_by_id.values()), ("itemId", "sprintId", "projectId")),
        "Item_Activity": write_csv("Item_Activity", raw_activities, ("itemId", "sprintId")),
        "Item_Sprint_Memberships": write_csv("Item_Sprint_Memberships", item_memberships),
        "Sync_Log": write_csv("Sync_Log", log),
        "Sync_Errors": write_csv("Sync_Errors", issues, ("stage", "id", "error")),
    }
    # Daily snapshots from current-state data are genuine as-of-today snapshots,
    # not reconstructed historical burndown data.
    snapshot_rows = [{"snapshot_utc": log[0]["sync_utc"], "sprintId": s.get("sprintId", ""),
                      "item_count": sum(1 for x in item_memberships if x["sprintId"] == s.get("sprintId"))}
                     for s in sprints]
    # Preserve earlier daily observations; one snapshot per sprint per UTC day.
    history_path = OUTPUT_DIR / "Daily_Snapshots.csv"
    if history_path.exists():
        with history_path.open(encoding="utf-8-sig", newline="") as file:
            historical = list(csv.DictReader(file))
        day = log[0]["sync_utc"][:10]
        historical = [r for r in historical if r.get("snapshot_utc", "")[:10] != day]
        snapshot_rows = historical + snapshot_rows
    tables["Daily_Snapshots"] = write_csv("Daily_Snapshots", snapshot_rows)
    return tables, len(issues)

def update_google_sheet(tables):
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not spreadsheet_id:
        print("GOOGLE_SHEET_ID not set; CSV export complete. Skipping Sheets upload.")
        return
    try:
        from google.auth import default
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Install 'google-api-python-client google-auth' for Sheets upload") from exc
    credentials, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {sheet["properties"]["title"] for sheet in metadata.get("sheets", [])}
    missing = [{"addSheet": {"properties": {"title": tab}}} for tab in tables if tab not in existing]
    if missing:
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": missing}).execute()
    for tab, rows in tables.items():
        # Replace, not append. This prevents duplicate data on subsequent runs.
        escaped = tab.replace("'", "''")
        range_name = f"'{escaped}'!A1"
        service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=f"'{escaped}'", body={}).execute()
        for start in range(0, len(rows), 1000):
            subrows = rows[start:start + 1000]
            destination = f"'{escaped}'!A{start + 1}"
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id, range=destination,
                valueInputOption="RAW", body={"values": subrows}).execute()
        print(f"Updated Google Sheets tab: {tab}")

async def main():
    url = os.environ.get("ZOHO_MCP_URL")
    if not url:
        raise RuntimeError("Set ZOHO_MCP_URL in your shell environment")
    oauth = OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="Offernet Sprints Sync",
            redirect_uris=[AnyUrl("http://localhost:8765/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"]),
        storage=InMemoryTokenStorage(),
        redirect_handler=open_browser,
        callback_handler=handle_callback,
    )
    # Request uncompressed data to avoid the intermittent decompression failure.
    # Use the supported upstream HTTPX client rather than the httpx2 fork.
    async with httpx2.AsyncClient(
        auth=oauth,
        headers={"Accept-Encoding": "identity"},
        timeout=120.0
    ) as client:
        async with streamable_http_client(url, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tables, errors = await sync_all(session)
    update_google_sheet(tables)
    print("Done. Check export/Sync_Errors.csv for failures.")
    if errors:
        print(f"WARNING: {errors} retrieval issues. Export is incomplete.")

if __name__ == "__main__":
    asyncio.run(main())
