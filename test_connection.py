
import asyncio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


async def main():
    url = os.environ.get("ZOHO_MCP_URL")

    if not url:
        raise ValueError("ZOHO_MCP_URL is not configured")

    print("Connecting to Zoho Sprints MCP...")

    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            try:
                await session.initialize()

            except MCPError as exc:
                print("\nMCP initialization failed.")
                print(f"Error code: {exc.code}")
                print("The server rejected the initialization request.")
                return

            print("MCP connection successful!")

            result = await session.list_tools()

            print(f"\nAvailable tools: {len(result.tools)}")

            for tool in result.tools:
                print(f"\nTool: {tool.name}")
                print(
                    f"Description: "
                    f"{(tool.description or '')[:300]}"
                )
                print(f"Input schema: {tool.inputSchema}")

    print("\nRead-only discovery completed.")


if __name__ == "__main__":
    asyncio.run(main())
