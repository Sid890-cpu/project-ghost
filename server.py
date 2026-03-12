from __future__ import annotations
import os, json, httpx, re
from datetime import datetime
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

async def fetch_url(url: str) -> tuple[str, str]:
    """Use Jina AI Reader to fetch any URL — bypasses Cloudflare and paywalls."""
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "text/plain",
        "X-No-Cache": "true",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=30.0
        ) as client:
            res = await client.get(jina_url)

        if res.status_code != 200:
            return "", f"HTTP {res.status_code}"

        text = res.text
        if not text or len(text.strip()) < 100:
            return "", "empty response"

        # Detect robot/captcha pages — check first 1500 chars
        first_chunk = text[:1500].lower()
        bot_signals = [
            "are you a robot", "are you human", "captcha", "unusual activity",
            "verify you are human", "cf-browser-verification", "access denied",
            "robot?", "please verify", "security check", "not a robot",
            "detected unusual", "suspicious activity"
        ]
        if any(b in first_chunk for b in bot_signals):
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

    # Simple fallback: extract capitalized words as entities from text
    def fallback_entities(t: str) -> list[str]:
        words = re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b', t[:3000])
        seen, unique = set(), []
        for w in words:
            if w not in seen and len(w) > 2:
                seen.add(w)
                unique.append(w)
        return unique[:8] or ["Unknown"]

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract business intelligence signals from web content as JSON. "
                        "Return exactly this structure with NO nulls and NO empty arrays:\n"
                        "{ business_intent (a full 1-2 sentence summary of what this page is about and why it matters), "
                        "priority_score (1-10), category, "
                        "items: [ { title, entities: [company/product/person names], impact_score (1-10) } ] }\n"
                        "items must have at least 2 entries. business_intent must be a complete descriptive sentence, not a single word."
                    )
                },
                {"role": "user", "content": f"Extract signals:\n\n{text[:6000]}"}
            ],
            response_format={"type": "json_object", "schema": StandardizedSignal.model_json_schema()}
        )
        ai_raw = json.loads(completion.choices[0].message.content)
        items = ai_raw.get("items", [])

        # If AI returned empty items, build fallback items from raw text
        if not items:
            entities = fallback_entities(text)
            items = [
                {"title": "Signal extracted from page", "entities": entities[:4], "impact_score": 5.0,
                 "published_time": datetime.utcnow().isoformat() + "Z"},
                {"title": "Additional intelligence", "entities": entities[4:8] or entities[:2], "impact_score": 4.0,
                 "published_time": datetime.utcnow().isoformat() + "Z"},
            ]

        entities_count = sum(len(i.get("entities", [])) for i in items)
        avg_impact = sum(i.get("impact_score", 5) for i in items) / max(len(items), 1)
        # Score based on: data richness (items + entities) + AI confidence (avg impact)
        # Max realistic breakdown: items(0-0.3) + entities(0-0.4) + impact(0-0.3)
        item_score = min(len(items) / 10, 0.3)
        entity_score = min(entities_count / 20, 0.4)
        impact_score = round((avg_impact / 10) * 0.3, 2)
        score = round(item_score + entity_score + impact_score, 2)

        return {
            "decision_signal": {
                "business_intent": ai_raw.get("business_intent") or "Intelligence extracted from page",
                "priority_score": ai_raw.get("priority_score") or 5.0,
                "category": ai_raw.get("category") or "GENERAL"
            },
            "items": items,
            "integrity_layer": {
                "confidence_score": score,
                "is_high_integrity": score > 0.7
            }
        }
    except Exception as e:
        # Full fallback — still return something useful
        entities = fallback_entities(text)
        return {
            "decision_signal": {
                "business_intent": "Page content extracted — AI processing failed",
                "priority_score": 3.0,
                "category": "GENERAL"
            },
            "items": [
                {"title": "Entities detected on page", "entities": entities[:5],
                 "impact_score": 4.0, "published_time": datetime.utcnow().isoformat() + "Z"},
            ],
            "integrity_layer": {"confidence_score": 0.4, "is_high_integrity": False}
        }

@mcp.tool
async def distill_web(url: str):
    text, error = await fetch_url(url)
    if error:
        return {
            "url": url,
            "title": "Blocked",
            "signals_data": {"error": "Scraper blocked", "integrity_layer": {"confidence_score": 0}}
        }

    # Jina response title — try multiple formats
    title = "No Title"
    for line in text.split('\n')[:15]:  # title is always in first 15 lines
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith('title:'):
            title = line[6:].strip()
            break
        if line.startswith('# '):
            title = line[2:].strip()
            break
        if line.lower().startswith('url source:') or line.lower().startswith('source:'):
            continue  # skip these lines
        # First non-empty, non-metadata line is probably the title
        if len(line) > 5 and not line.startswith('http') and not line.startswith('['):
            title = line.lstrip('#').strip()
            break

    # Extract just the main content (after "Markdown Content:" if present)
    if 'Markdown Content:' in text:
        content_text = text.split('Markdown Content:', 1)[1].strip()
    else:
        content_text = text

    # Clean up excessive whitespace
    clean_text = ' '.join(content_text.split())

    # Tokens saved = raw Jina response (before content extraction) vs what we send to Groq
    # This is the honest number: full page text Jina fetched vs 6000 chars we process
    raw_jina_len = len(text)  # full Jina response including headers/metadata
    sent_to_ai = min(len(clean_text), 6000)
    savings = f"{round((1 - sent_to_ai / max(raw_jina_len, 1)) * 100, 1)}%"

    # Detect robot/captcha pages — return blocked instead of processing junk
    robot_signals = ["are you a robot", "captcha", "verify you are human", "unusual activity", "access denied", "enable javascript"]
    if any(s in title.lower() for s in robot_signals) or any(s in clean_text[:500].lower() for s in robot_signals):
        return {
            "url": url,
            "title": "Blocked",
            "signals_data": {"error": "Scraper blocked", "integrity_layer": {"confidence_score": 0}}
        }

    signals = await get_hybrid_intelligence(clean_text)

    payload = {
        "url": url,
        "title": title[:200],
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