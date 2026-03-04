import asyncio
from fastmcp import Client


async def main():
    async with Client("http://localhost:8000/mcp") as client:
        result = await client.call_tool(
            "distill_web",
            {"url": "https://news.ycombinator.com"},
        )
        print("Result from distill_web:\n")
        print(result)


if __name__ == "__main__":
    asyncio.run(main())

