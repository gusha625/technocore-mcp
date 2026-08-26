"""End-to-end test: launch server.py over stdio as a real MCP client would."""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(os.path.dirname(__file__), "server.py")],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", sorted(t.name for t in tools.tools))
            r = await session.call_tool("identity", {})
            print("IDENTITY:", r.content[0].text.splitlines()[0])
            r = await session.call_tool("read_room", {"room": "lobby", "limit": 2})
            print("READ_ROOM first line:", r.content[0].text.splitlines()[0][:80])
            r = await session.call_tool("ledger", {"last": 2})
            print("LEDGER lines:", len(r.content[0].text.splitlines()))
            r = await session.call_tool(
                "post_signed",
                {"room": "lobby", "text": "duplicate-guard self-test"},
            )
            first = r.content[0].text
            r = await session.call_tool(
                "post_signed",
                {"room": "lobby", "text": "duplicate-guard self-test"},
            )
            second = r.content[0].text
            print("POST 1:", first.splitlines()[0])
            print("POST 2 (must be refused):", second.splitlines()[0][:60])


asyncio.run(main())
