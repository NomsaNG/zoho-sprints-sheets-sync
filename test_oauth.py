
import asyncio
import json
import os
import webbrowser

import httpx2

from pydantic import AnyUrl
from urllib.parse import urlparse, parse_qs

from mcp import ClientSession
from mcp.client.auth import (
    AuthorizationCodeResult,
    OAuthClientProvider,
)
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata


# ---------------------------------------
# OAuth Token Storage (Development Only)
# ---------------------------------------

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


# ---------------------------------------
# OAuth Browser Authorization
# ---------------------------------------

async def open_browser(auth_url):
    print("Opening Zoho OAuth authorization...")
    webbrowser.open(auth_url)


async def handle_callback():
    print("\nComplete authorization in your browser.")
    print("If localhost refuses to connect, copy the")
    print("callback URL into this terminal.")
    print("Never share the URL or authorization code.")

    callback_url = input("\nCallback URL: ").strip()
    parsed = urlparse(callback_url)

    if (
        parsed.scheme != "http"
        or parsed.hostname != "localhost"
        or parsed.port != 8765
        or parsed.path != "/callback"
    ):
        raise ValueError("Unexpected callback address")

    params = parse_qs(parsed.query)

    if "error" in params:
        raise RuntimeError("Zoho authorization was rejected")

    if "code" not in params or "state" not in params:
        raise ValueError("Missing OAuth code or state")

    return AuthorizationCodeResult(
        code=params["code"][0],
        state=params["state"][0],
        iss=params.get("iss", [None])[0],
    )


# ---------------------------------------
# Handle MCP Tool Responses
# ---------------------------------------

def unpack_result(result):
    if result.is_error:
        raise RuntimeError("Zoho MCP returned a tool error")

    if result.structured_content is not None:
        return result.structured_content

    values = []

    for block in result.content:
        if block.type == "text":
            try:
                values.append(json.loads(block.text))
            except json.JSONDecodeError:
                values.append(block.text)

    return values[0] if len(values) == 1 else values


# ---------------------------------------
# MCP Connection and Workspace Extraction
# ---------------------------------------

async def main():
    url = os.environ.get("ZOHO_MCP_URL")

    if not url:
        raise ValueError("ZOHO_MCP_URL is not configured")

    oauth = OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="Offernet Sprints Sync",
            redirect_uris=[
                AnyUrl("http://localhost:8765/callback")
            ],
            grant_types=[
                "authorization_code",
                "refresh_token",
            ],
            response_types=["code"],
        ),
        storage=InMemoryTokenStorage(),
        redirect_handler=open_browser,
        callback_handler=handle_callback,
    )

    print("Connecting with Zoho OAuth...")

    async with httpx2.AsyncClient(auth=oauth) as client:
        async with streamable_http_client(
            url,
            http_client=client,
        ) as (read, write):

            async with ClientSession(read, write) as session:
                await session.initialize()

                print("\nMCP CONNECTION SUCCESSFUL!")

                # Fetch Zoho Sprints workspaces
                print("\nFetching Zoho Sprints workspaces...")

                result = await session.call_tool(
                    "ZohoSprints_getAllWorkspaces",
                    arguments={}
                )

                workspaces = unpack_result(result)

                # Save response locally
                output_path = "workspaces_response.json"

                with open(output_path, "w") as file:
                    json.dump(
                        workspaces,
                        file,
                        indent=2,
                        default=str
                    )

                print(
                    "Workspace response saved to:",
                    output_path
                )

                print("Workspace extraction completed.")
                print(
                    "\nNext: identify teamId before "
                    "retrieving TouchPoint projects."
                )


                # -----------------------------------
                # Fetch projects in the workspace
                # -----------------------------------

                team_id = "919475195"

                print("\nFetching projects from codeworks...")

                project_result = await session.call_tool(
                    "ZohoSprints_GetProjects",
                    arguments={
                        "headers": {
                            "x-za-ui-version": "v2",
                            "X-convert-response": "true"
                        },
                        "path_variables": {
                            "teamId": team_id
                        },
                        "query_params": {
                            "action": "data",
                            "index": 1,
                            "range": 250,
                            "projectstatus": 1
                        }
                    }
                )

                projects = unpack_result(project_result)

                output_path = "projects_response.json"

                with open(output_path, "w") as file:
                    json.dump(
                        projects,
                        file,
                        indent=2,
                        default=str
                    )

                print("Projects saved to:", output_path)
                print("Project extraction completed.")


                # -----------------------------------
                # Fetch all TouchPoint sprints
                # -----------------------------------

                project_id = "178933000000009002"

                print("\nFetching TouchPoint sprints...")

                all_sprints = []
                index = 1
                page_size = 250

                while True:
                    sprint_result = await session.call_tool(
                        "ZohoSprints_GetSprints",
                        arguments={
                            "headers": {
                                "x-za-ui-version": "v2",
                                "X-convert-response": "true"
                            },
                            "path_variables": {
                                "teamId": team_id,
                                "projectId": project_id
                            },
                            "query_params": {
                                "action": "data",
                                "index": index,
                                "range": page_size,
                                "type": "[1,2,3,4]"
                            }
                        }
                    )

                    response = unpack_result(sprint_result)

                    if (
                        not isinstance(response, dict)
                        or response.get("status") != "success"
                    ):
                        raise RuntimeError(
                            "Unexpected sprint response"
                        )

                    data = response.get("data", {})

                    # Inspect response structure before
                    # implementing full pagination.
                    if index == 1:
                        with open(
                            "sprints_raw_response.json", "w"
                        ) as file:
                            json.dump(
                                response,
                                file,
                                indent=2,
                                default=str
                            )

                        print(
                            "First response saved to "
                            "sprints_raw_response.json"
                        )

                    # Stop after the first page until
                    # we confirm the collection key.
                    break

                print("Sprint retrieval test completed.")

if __name__ == "__main__":
    asyncio.run(main())
