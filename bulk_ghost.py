import asyncio
import json
from fastmcp import Client

target_urls = [
    "https://news.ycombinator.com",
    "https://techcrunch.com",
    "https://www.theverge.com",
    "https://timesofindia.indiatimes.com",
    "https://www.bbc.com/news"
]

async def distill_task(client, url):
    print(f"👻 Distilling: {url}...")
    try:
        # Call the tool and unpack the MCP 'CallToolResult' object
        call_result = await client.call_tool("distill_web", {"url": url})
        
        # This converts the server's text response back into a Python dictionary
        result = json.loads(call_result.content[0].text)
        
        print(f"✅ Success: {url}")
        return result
    except Exception as e:
        print(f"❌ Error on {url}: {e}")
        return None

async def main():
    # Increase timeout to 120 seconds for Level 2 AI analysis
    async with Client("http://localhost:8000/mcp", timeout=120) as client:
        print("🚀 Starting Bulk Distillation Factory (High-Intelligence Mode)...\n")
        tasks = [distill_task(client, url) for url in target_urls]
        # ... rest of your code ...
        
        results = await asyncio.gather(*tasks)
        
        # Clean out any None results from errors
        clean_results = [r for r in results if r is not None]
        
        # Save the structured data to your local JSON file
        file_name = "distilled_data.json"
        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(clean_results, f, indent=4)
            
        print(f"\n✨ DONE! {len(clean_results)} sites saved to Supabase and '{file_name}'")

if __name__ == "__main__":
    asyncio.run(main())