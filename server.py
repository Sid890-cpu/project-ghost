from __future__ import annotations
import os, json, httpx, re, random
from datetime import datetime
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()
mcp = FastMCP(name="project-ghost")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

def get_supabase():
    return create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_ANON_KEY"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

async def fetch_url(url: str) -> tuple[str, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    }
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as client:
            res = await client.get(url)
        if res.status_code != 200:
            return "", f"HTTP {res.status_code}"
        text = res.text
        block_signals = ["cf-browser-verification", "captcha", "access denied", "403 forbidden", "enable javascript"]
        if any(b in text.lower() for b in block_signals):
            return "", "blocked"
        return text, ""
    except Exception as e:
        return "", str(e)

async def get_hybrid_intelligence(text: str):
    class StandardizedItem(BaseModel):
        title: str
        published_time: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
        entities: list[str] = Field(min_items=1)
        impact_score: float = Field(ge=1.0, le=10.0)

    class StandardizedSignal(BaseModel):
        business_intent: str
        priority_score: float = Field(ge=1.0, le=10.0)
        category: str
        items: list[StandardizedItem] = Field(min_items=2)

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a business intelligence extractor. "
                        "Extract structured signals from web content. "
                        "Always return valid JSON with at least 2 items. "
                        "Each item must have: title (headline), entities (list of companies/people/products), impact_score (1-10). "
                        "DO NOT RETURN NULL OR EMPTY ARRAYS."
                    )
                },
                {"role": "user", "content": f"Extract business signals from this content:\n\n{text[:8000]}"}
            ],
            response_format={"type": "json_object", "schema": StandardizedSignal.model_json_schema()}
        )
        ai_raw = json.loads(completion.choices[0].message.content)
        items = ai_raw.get("items", [])
        entities_count = len(items[0].get("entities", [])) if items else 0
        score = round(min((len(items) * 0.1) + (entities_count * 0.15) + 0.4, 1.0), 2)
        return {
            "decision_signal": {
                "business_intent": ai_raw.get("business_intent"),
                "priority_score": ai_raw.get("priority_score"),
                "category": ai_raw.get("category")
            },
            "items": items,
            "integrity_layer": {
                "confidence_score": score,
                "is_high_integrity": score > 0.7
            }
        }
    except Exception as e:
        return {"error": str(e), "integrity_layer": {"confidence_score": 0, "is_high_integrity": False}}

@mcp.tool
async def distill_web(url: str):
    html, error = await fetch_url(url)
    if error:
        return {
            "url": url,
            "title": "Blocked",
            "signals_data": {"error": "Scraper blocked", "integrity_layer": {"confidence_score": 0}}
        }
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    clean_text = " ".join(soup.get_text().split())
    savings = f"{round((1 - (len(clean_text) / max(len(html), 1))) * 100, 1)}%"
    signals = await get_hybrid_intelligence(clean_text)
    payload = {
        "url": url,
        "title": soup.title.string.strip() if soup.title and soup.title.string else "No Title",
        "content": clean_text[:2000],
        "signals_data": signals,
        "tokens_saved": savings,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    try:
        get_supabase().table("ghost_memory").insert(payload).execute()
    except Exception:
        pass
    return payload

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    async def http_distill(request: Request):
        try:
            body = await request.json()
            url = body.get("url")
            if not url:
                return JSONResponse({"error": "url required"}, status_code=400)
            result = await distill_web(url)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def http_health(request: Request):
        return JSONResponse({"status": "ok", "version": "2.0"})

    mcp_app = mcp.http_app()
    app = Starlette(routes=[
        Route("/distill", http_distill, methods=["POST"]),
        Route("/health", http_health, methods=["GET"]),
        Mount("/", app=mcp_app),
    ])
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)