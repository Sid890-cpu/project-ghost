from __future__ import annotations
import os, json, httpx, re
from datetime import datetime
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()
mcp = FastMCP(name="project-ghost")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

def get_supabase():
    return create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_ANON_KEY"))

async def get_hybrid_intelligence(text: str):
    # THE CODE-FIXED SCHEMA (Ensures agents get perfect data)
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
                {"role": "system", "content": "Extract signals exactly per JSON schema. DO NOT RETURN NULL."},
                {"role": "user", "content": f"Refine into JSON: {text[:8000]}"}
            ],
            response_format={"type": "json_object", "schema": StandardizedSignal.model_json_schema()}
        )
        
        ai_raw = json.loads(completion.choices[0].message.content)
        
        # Dynamic Integrity Math: Calculates score based on found data
        items = ai_raw.get('items', [])
        entities_count = len(items[0].get('entities', [])) if items else 0
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
    # B2B Headers to stop Bloomberg/WSJ from blocking you
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as client:
        res = await client.get(url)
    
    # Robot Defense: Stops empty AI runs if the site blocks the scraper
    if "robot" in res.text.lower() or "denied" in res.text.lower() or res.status_code != 200:
        return {
            "url": url,
            "title": "Blocked",
            "signals_data": {"error": "Scraper blocked", "integrity_layer": {"confidence_score": 0}}
        }

    soup = BeautifulSoup(res.text, "html.parser")
    clean_text = " ".join(soup.get_text().split())
    savings = f"{round((1 - (len(clean_text) / len(res.text))) * 100, 1)}%"
    
    # Process signals through the upgraded Hybrid Intelligence
    signals = await get_hybrid_intelligence(clean_text)
    
    payload = {
       "url": url,
        "title": soup.title.string if soup.title else "No Title",
        "content": clean_text[:2000],
        "signals_data": signals, 
        "tokens_saved": savings,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    
    # Build your library while you sleep
    get_supabase().table("ghost_memory").insert(payload).execute()
    return payload

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    # HTTP /distill endpoint for browser calls
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

    # Get the FastMCP ASGI app
    mcp_app = mcp.http_app()

    # Mount CORS around a Starlette app that includes both /distill and MCP routes
    from starlette.routing import Mount
    app = Starlette(routes=[
        Route("/distill", http_distill, methods=["POST"]),
        Route("/health", http_health, methods=["GET"]),
        Mount("/", app=mcp_app),
    ])

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)