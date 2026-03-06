from __future__ import annotations
import os, json, httpx, re
from datetime import datetime
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
mcp = FastMCP(name="project-ghost")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# 1. THE UNIVERSAL BUSINESS SCHEMA (The "No-Prompt" Form)
class UniversalSignal(BaseModel):
    key_entities: list[str] = Field(description="Top 3 companies, countries, or people found")
    business_intent: str = Field(description="Commercial, Financial, Geopolitical, or Technical")
    action_priority: int = Field(ge=1, le=10, description="1=Low, 10=Immediate action required")
    sentiment_score: float = Field(description="Sentiment from -1.0 to 1.0")

def get_supabase():
    return create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_ANON_KEY"))

async def get_hybrid_intelligence(text: str):
    # 2. MACHINE-TO-MACHINE EXTRACTION
    # We don't use a long prompt; we just tell Groq to fill the 'UniversalSignal' form.
    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": f"Extract signal from: {text[:6000]}"}],
        response_format={
            "type": "json_object",
            "schema": UniversalSignal.model_json_schema()
        }
    )
    return json.loads(completion.choices[0].message.content)

@mcp.tool
async def distill_web(url: str):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await client.get(url)
    
    soup = BeautifulSoup(res.text, "html.parser")
    clean_text = " ".join(soup.get_text().split())
    
    # Get the high-precision signal
    signals = await get_hybrid_intelligence(clean_text)
    
    payload = {
       "url": url,
        "title": soup.title.string if soup.title else "No Title",
        "content": clean_text[:2000],
        "signals_data": signals, # Saves the entire nested JSON into one column
        "tokens_saved": savings,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    
    # Build your library while you sleep
    get_supabase().table("ghost_memory").insert(payload).execute()
    return payload

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    mcp.run(transport="http", host="0.0.0.0", port=port)