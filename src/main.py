"""Read-only Zoho Sprints MCP discovery and optional Google Sheets export.

Only a named read tool is invoked in sync mode; discovery never invokes tools.
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger(__name__)

READ_PREFIXES = ("get_", "list_", "fetch_", "search_", "retrieve_", "view_")
SENSITIVE = re.compile(r"(key|token|secret|password|authorization)", re.I)


def jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def check_read_tool(tool_name):
    # Exact tool name is required and a conservative name check prevents accidental writes.
    if not tool_name.lower().startswith(READ_PREFIXES):
        raise ValueError("Refusing non-read-looking tool. Expected get_, list_, fetch_, search_, retrieve_, or view_.")


def flatten_content(result):
    if getattr(result, "isError", False):
        raise RuntimeError("The selected MCP tool reported an error; no data was written")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return jsonable(structured)
    blocks = []
    for c in (getattr(result, "content", None) or []):
        if getattr(c, "type", "") == "text":
            raw = c.text
            try:
                blocks.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                blocks.append(raw)
    return blocks[0] if len(blocks) == 1 else blocks


def write_sheet(payload, spreadsheet_id, tab_name):
    from google.auth import default
    from googleapiclient.discovery import build

    # Raw export preserves nested response data without making assumptions about schema.
    records = payload if isinstance(payload, list) else [payload]
    now = datetime.now(timezone.utc).isoformat()
    rows = [["extracted_at_utc", "record_number", "payload_json"]]
    rows.extend([now, i, json.dumps(item, ensure_ascii=False, default=str)] for i, item in enumerate(records, 1))
    credentials, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    # Tab must be created beforehand. Clear then replace in one execution; no append duplicates.
    quoted_tab = "'" + tab_name.replace("'", "''") + "'"
    target = f"{quoted_tab}!A:C"
    service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=target, body={}).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"{quoted_tab}!A1",
        valueInputOption="RAW", body={"values": rows}
    ).execute()
    LOG.info("Published %s records into tab %s", len(records), tab_name)


async def run():
    mode = os.getenv("MODE", "discover").lower()
    if mode not in ("discover", "sync"):
        raise ValueError("MODE must be discover or sync")
    endpoint = os.getenv("ZOHO_MCP_URL", "").strip()
    if not endpoint.startswith("https://"):
        raise ValueError("Set ZOHO_MCP_URL to the complete HTTPS MCP URL via Secret Manager or local environment")

    LOG.info("Connecting to Zoho MCP (endpoint deliberately redacted)")
    async with streamablehttp_client(endpoint, timeout=30) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            tools = result.tools
            LOG.info("MCP handshake succeeded; %s tools visible", len(tools))
            for tool in tools:
                flags = "read candidate" if tool.name.lower().startswith(READ_PREFIXES) else "not auto-approved"
                LOG.info("TOOL %s [%s]", tool.name, flags)
                # Tool schema can contain unusual information; print only the actual field names.
                schema = jsonable(tool.inputSchema) if getattr(tool, "inputSchema", None) else {}
                LOG.info("  required=%s properties=%s", schema.get("required", []), list(schema.get("properties", {}).keys()))
            if mode == "discover":
                LOG.info("Discovery finished: no Zoho tool was invoked and no Sheet was changed")
                return
            tool_name = os.getenv("MCP_READ_TOOL", "").strip()
            check_read_tool(tool_name)
            if tool_name not in {tool.name for tool in tools}:
                raise ValueError("MCP_READ_TOOL is not advertised by this server")
            supplied_args = os.getenv("MCP_TOOL_ARGS_JSON", "{}")
            args = json.loads(supplied_args)
            if not isinstance(args, dict):
                raise ValueError("MCP_TOOL_ARGS_JSON must be a JSON object")
            LOG.info("Calling selected read tool: %s", tool_name)
            response = await session.call_tool(tool_name, arguments=args)
            payload = flatten_content(response)
            # Do not log records, since task data may contain confidential information.
            spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()
            if not spreadsheet_id:
                LOG.info("Read succeeded (no GOOGLE_SPREADSHEET_ID supplied); no data was written")
                return
            tab = os.getenv("GOOGLE_SHEET_TAB", "Raw_MCP_Export")
            write_sheet(payload, spreadsheet_id, tab)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as exc:
        # Exception messages from HTTP libraries could embed URLs with credentials: never print them.
        LOG.error("Execution failed (%s). Check endpoint, access grants, MCP tool args and Cloud Logs.", type(exc).__name__)
        sys.exit(1)
