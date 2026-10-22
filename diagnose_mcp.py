
import os
import httpx

url = os.environ.get("ZOHO_MCP_URL")

if not url:
    raise ValueError("ZOHO_MCP_URL is not configured")

payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {
            "name": "offernet-sprints-diagnostic",
            "version": "1.0.0"
        }
    }
}

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

try:
    with httpx.Client(
        timeout=30,
        follow_redirects=False
    ) as client:

        with client.stream(
            "POST",
            url,
            json=payload,
            headers=headers
        ) as response:

            print("HTTP status:", response.status_code)
            print(
                "Content type:",
                response.headers.get("content-type")
            )

            print(
                "Authentication rejected:",
                response.status_code == 401
            )

            print(
                "Request accepted:",
                response.status_code == 200
            )

except httpx.RequestError as exc:
    print("Connection error:", type(exc).__name__)
