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

import secrets, hashlib

PLAN_LIMITS = {
    "free":      100,
    "developer": 2000,
    "startup":   10000,
    "unlimited": 999999,
}

def generate_api_key() -> str:
    """Generate a new ghost_sk_ prefixed API key."""
    return "ghost_sk_" + secrets.token_urlsafe(32)

def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

async def validate_api_key(request: Request):
    """Check Authorization header. Returns (is_valid, error_response, key_row)"""
    master_key = os.environ.get("GHOST_API_KEY", "")
    auth_header = request.headers.get("Authorization", "")
    provided_key = auth_header.replace("Bearer ", "").strip()

    if not provided_key:
        return False, JSONResponse(
            {"error": "API key required", "hint": "Add header: Authorization: Bearer ghost_sk_..."},
            status_code=401
        ), None

    if master_key and provided_key == master_key:
        return True, None, {"plan": "unlimited", "requests_used": 0, "id": "master"}

    try:
        sb = get_supabase()
        key_hash = hash_key(provided_key)
        result = sb.table("api_keys").select("*").eq("key_hash", key_hash).eq("is_active", True).execute()

        if not result.data:
            return False, JSONResponse({"error": "Invalid or inactive API key"}, status_code=401), None

        row = result.data[0]
        plan = row.get("plan", "free")
        limit = PLAN_LIMITS.get(plan, 100)
        used = row.get("requests_used", 0)

        if used >= limit:
            return False, JSONResponse(
                {"error": "Rate limit exceeded", "plan": plan, "limit": limit, "used": used,
                 "hint": "Upgrade at https://project-ghost-lilac.vercel.app"},
                status_code=429
            ), None

        return True, None, row

    except Exception as e:
        print(f"[API KEY CHECK ERROR] {e}")
        return True, None, {"plan": "free", "requests_used": 0, "id": "unknown"}

async def increment_usage(key_id: str):
    if key_id == "master":
        return
    try:
        sb = get_supabase()
        sb.rpc("increment_key_usage", {"key_id": key_id}).execute()
    except Exception as e:
        print(f"[USAGE INCREMENT ERROR] {e}")


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
        # Validate API key
        is_valid, err_response, key_row = await validate_api_key(request)
        if not is_valid:
            return err_response
        try:
            body = await request.json()
            url = body.get("url")
            if not url:
                return JSONResponse({"error": "url required"}, status_code=400)
            result = await distill_web(url)
            # Track usage
            await increment_usage(key_row.get("id", "unknown"))
            # Add usage info to response
            plan = key_row.get("plan", "free")
            used = key_row.get("requests_used", 0) + 1
            limit = PLAN_LIMITS.get(plan, 100)
            result["_usage"] = {"plan": plan, "requests_used": used, "limit": limit, "remaining": limit - used}
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def http_generate_key(request: Request):
        """Admin endpoint to generate API keys. Protected by master key."""
        master_key = os.environ.get("GHOST_API_KEY", "")
        auth = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not master_key or auth != master_key:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
            email = body.get("email", "unknown")
            plan = body.get("plan", "free")
            name = body.get("name", "")
            new_key = generate_api_key()
            key_hash = hash_key(new_key)
            sb = get_supabase()
            sb.table("api_keys").insert({
                "key_hash": key_hash,
                "email": email,
                "name": name,
                "plan": plan,
                "is_active": True,
                "requests_used": 0,
                "created_at": datetime.utcnow().isoformat() + "Z"
            }).execute()
            return JSONResponse({"key": new_key, "email": email, "plan": plan,
                                  "limit": PLAN_LIMITS.get(plan, 100),
                                  "message": "Send this key to the developer. It won't be shown again."})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def http_health(request: Request):
        return JSONResponse({"status": "ok", "version": "2.0"})

    async def http_server_card(request: Request):
        from starlette.responses import JSONResponse as JR
        return JR({
            "name": "Project Ghost",
            "description": "Web reading layer for AI agents. Convert any public URL into structured, agent-ready data — entities, signals, summary — in one tool call.",
            "version": "2.0.0",
            "url": "https://project-ghost-production.up.railway.app/mcp",
            "homepage": "https://project-ghost-lilac.vercel.app",
            "tools": [
                {
                    "name": "distill_web",
                    "description": "Convert any public URL into structured intelligence. Returns title, summary, entities, confidence score and tokens saved.",
                    "parameters": {
                        "url": {
                            "type": "string",
                            "description": "Any publicly accessible URL to extract intelligence from",
                            "required": True
                        }
                    }
                }
            ],
            "authentication": {
                "type": "api_key",
                "description": "Get your free API key at https://project-ghost-lilac.vercel.app"
            }
        })

    async def http_root(request: Request):
        from starlette.responses import HTMLResponse
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Project Ghost API Docs</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e1a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.top-bar{background:#020305;border-bottom:2px solid #00e5a0;padding:16px 32px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100}
.top-logo{font-size:20px;font-weight:800;color:#fff;letter-spacing:-0.5px}
.top-logo span{color:#00e5a0}
.top-version{background:rgba(0,229,160,0.1);border:1px solid rgba(0,229,160,0.3);color:#00e5a0;font-size:11px;padding:3px 10px;border-radius:20px;font-family:monospace}
.top-live{display:flex;align-items:center;gap:6px;margin-left:auto;font-size:12px;color:#00e5a0}
.live-dot{width:8px;height:8px;background:#00e5a0;border-radius:50%;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.layout{display:grid;grid-template-columns:260px 1fr;min-height:calc(100vh - 57px)}
.sidebar{background:#020305;border-right:1px solid #1e293b;padding:24px 0;position:sticky;top:57px;height:calc(100vh - 57px);overflow-y:auto}
.sidebar-section{padding:8px 20px;font-size:10px;color:#475569;letter-spacing:2px;text-transform:uppercase;margin-top:16px}
.sidebar-item{display:block;padding:10px 20px;font-size:13px;color:#94a3b8;text-decoration:none;border-left:3px solid transparent;transition:all 0.2s;cursor:pointer}
.sidebar-item:hover,.sidebar-item.active{color:#fff;background:rgba(0,229,160,0.05);border-left-color:#00e5a0}
.sidebar-method{display:inline-block;font-size:10px;font-weight:700;padding:2px 6px;border-radius:3px;margin-right:8px;font-family:monospace}
.sm-post{background:rgba(0,229,160,0.15);color:#00e5a0}
.sm-get{background:rgba(59,130,246,0.15);color:#60a5fa}
.sm-mcp{background:rgba(168,85,247,0.15);color:#c084fc}
.main{padding:40px 48px;max-width:900px}
.api-hero{margin-bottom:48px;padding-bottom:32px;border-bottom:1px solid #1e293b}
.api-hero h1{font-size:32px;font-weight:800;color:#fff;margin-bottom:8px}
.api-hero p{color:#64748b;font-size:15px;line-height:1.7;max-width:600px;margin-top:12px}
.base-url{background:#020305;border:1px solid #1e293b;border-radius:8px;padding:14px 20px;margin-top:20px;font-family:monospace;font-size:13px;color:#00e5a0;display:flex;align-items:center;gap:10px}
.base-url-label{color:#475569;font-size:11px;letter-spacing:1px}
.endpoint-block{margin-bottom:32px;border:1px solid #1e293b;border-radius:12px;overflow:hidden}
.endpoint-header{padding:20px 24px;display:flex;align-items:center;gap:16px;cursor:pointer;background:#0d1320;transition:background 0.2s}
.endpoint-header:hover{background:#111827}
.method-badge{padding:5px 14px;border-radius:6px;font-size:12px;font-weight:800;font-family:monospace;letter-spacing:0.5px;min-width:70px;text-align:center}
.badge-post{background:rgba(0,229,160,0.15);color:#00e5a0;border:1px solid rgba(0,229,160,0.3)}
.badge-get{background:rgba(59,130,246,0.15);color:#60a5fa;border:1px solid rgba(59,130,246,0.3)}
.badge-mcp{background:rgba(168,85,247,0.15);color:#c084fc;border:1px solid rgba(168,85,247,0.3)}
.endpoint-path{font-family:monospace;font-size:16px;font-weight:600;color:#fff}
.endpoint-summary{font-size:13px;color:#64748b;margin-left:auto}
.endpoint-chevron{color:#475569;font-size:12px;transition:transform 0.3s;margin-left:8px}
.endpoint-body{display:none;border-top:1px solid #1e293b}
.endpoint-body.open{display:block}
.ep-section{padding:24px;border-bottom:1px solid #0f172a}
.ep-section:last-child{border-bottom:none}
.ep-label{font-size:11px;color:#475569;letter-spacing:2px;text-transform:uppercase;margin-bottom:12px}
.ep-desc{font-size:14px;color:#94a3b8;line-height:1.7}
.param-row{display:grid;grid-template-columns:140px 80px 1fr;gap:16px;padding:10px 0;border-bottom:1px solid #0f172a;align-items:start}
.param-row:last-child{border-bottom:none}
.param-name{font-family:monospace;font-size:13px;color:#fff}
.param-type{font-family:monospace;font-size:11px;color:#f59e0b;background:rgba(245,158,11,0.1);padding:2px 8px;border-radius:4px;width:fit-content}
.param-req{font-size:10px;color:#ef4444;background:rgba(239,68,68,0.1);padding:1px 6px;border-radius:3px;margin-left:6px}
.param-desc{font-size:13px;color:#64748b}
.code-block{background:#020305;border-radius:8px;overflow:hidden;margin-top:8px}
.code-tabs{display:flex;border-bottom:1px solid #1e293b}
.code-tab{padding:8px 16px;font-size:12px;color:#64748b;cursor:pointer;border-bottom:2px solid transparent;transition:all 0.2s}
.code-tab.active{color:#00e5a0;border-bottom-color:#00e5a0}
.code-content{padding:20px;font-family:monospace;font-size:12px;line-height:1.8;overflow-x:auto}
.code-content .key{color:#60a5fa}
.code-content .str{color:#00e5a0}
.code-content .num{color:#f59e0b}
.code-content .comment{color:#475569}
.response-badge{display:inline-flex;align-items:center;gap:8px;background:rgba(0,229,160,0.05);border:1px solid rgba(0,229,160,0.2);padding:6px 14px;border-radius:6px;margin-bottom:12px}
.r-code{font-family:monospace;font-size:13px;color:#00e5a0;font-weight:700}
.r-desc{font-size:12px;color:#64748b}
.section-title{font-size:22px;font-weight:700;color:#fff;margin-bottom:8px;margin-top:48px;padding-top:48px;border-top:1px solid #1e293b}
.section-sub{font-size:14px;color:#64748b;margin-bottom:24px}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:32px}
.info-card{background:#0d1320;border:1px solid #1e293b;border-radius:10px;padding:20px}
.info-card-title{font-size:12px;color:#475569;letter-spacing:1px;margin-bottom:8px;text-transform:uppercase}
.info-card-val{font-size:20px;font-weight:700;color:#00e5a0;font-family:monospace}
.info-card-sub{font-size:12px;color:#64748b;margin-top:4px}
@media(max-width:768px){
  .layout{grid-template-columns:1fr}
  .sidebar{display:none}
  .main{padding:20px 16px}
  .api-hero h1{font-size:24px}
  .info-grid{grid-template-columns:1fr 1fr}
  .endpoint-header{flex-wrap:wrap;gap:10px}
  .endpoint-summary{display:none}
  .endpoint-path{font-size:14px}
  .param-row{grid-template-columns:1fr;gap:4px}
  .code-content{font-size:11px;padding:14px}
  .section-title{font-size:18px;padding-top:32px;margin-top:32px}
  .top-bar{padding:12px 16px}
  .mobile-nav{display:flex}
}
.mobile-nav{display:none;overflow-x:auto;gap:8px;padding:12px 16px;background:#020305;border-bottom:1px solid #1e293b;scrollbar-width:none}
.mobile-nav::-webkit-scrollbar{display:none}
.mobile-nav-item{flex-shrink:0;display:flex;align-items:center;gap:6px;padding:6px 14px;border:1px solid #1e293b;border-radius:20px;font-size:12px;color:#94a3b8;cursor:pointer;white-space:nowrap;transition:all 0.2s}
.mobile-nav-item:hover{border-color:#00e5a0;color:#fff}
</style>
</head>
<body>
<div class="top-bar">
  <div class="top-logo">Project<span>Ghost</span></div>
  <span class="top-version">v2.0</span>
  <div class="top-live"><div class="live-dot"></div> API Live</div>
</div>
<div class="layout">
  <div class="mobile-nav">
    <div class="mobile-nav-item" onclick="scrollTo('overview')">Intro</div>
    <div class="mobile-nav-item" onclick="scrollTo('quickstart')">Quick Start</div>
    <div class="mobile-nav-item" onclick="scrollTo('distill')"><span class="sidebar-method sm-post">POST</span>/distill</div>
    <div class="mobile-nav-item" onclick="scrollTo('health')"><span class="sidebar-method sm-get">GET</span>/health</div>
    <div class="mobile-nav-item" onclick="scrollTo('feed')"><span class="sidebar-method sm-get">GET</span>/feed</div>
    <div class="mobile-nav-item" onclick="scrollTo('search')"><span class="sidebar-method sm-get">GET</span>/search</div>
    <div class="mobile-nav-item" onclick="scrollTo('mcp')"><span class="sidebar-method sm-mcp">MCP</span>/mcp</div>
  </div>
  <div class="sidebar">
    <div class="sidebar-section">Overview</div>
    <a class="sidebar-item active" onclick="scrollTo('overview')">Introduction</a>
    <a class="sidebar-item" onclick="scrollTo('quickstart')">Quick Start</a>
    <div class="sidebar-section">Endpoints</div>
    <a class="sidebar-item" onclick="scrollTo('distill')"><span class="sidebar-method sm-post">POST</span>/distill</a>
    <a class="sidebar-item" onclick="scrollTo('health')"><span class="sidebar-method sm-get">GET</span>/health</a>
    <a class="sidebar-item" onclick="scrollTo('feed')"><span class="sidebar-method sm-get">GET</span>/feed</a>
    <a class="sidebar-item" onclick="scrollTo('search')"><span class="sidebar-method sm-get">GET</span>/search</a>
    <a class="sidebar-item" onclick="scrollTo('mcp')"><span class="sidebar-method sm-mcp">MCP</span>/mcp</a>
    <div class="sidebar-section">Resources</div>
    <a class="sidebar-item" href="https://project-ghost-lilac.vercel.app" target="_blank">Landing Page ↗</a>
    <a class="sidebar-item" href="mailto:ProjectGhost__@outlook.com">Contact</a>
  </div>
  <div class="main">
    <div class="api-hero" id="overview">
      <h1>Project Ghost API</h1>
      <p>The web reading layer for AI agents. Convert any public URL into structured, agent-ready data — entities, signals, summaries — in a single API call.</p>
      <div class="base-url">
        <span class="base-url-label">BASE URL</span>
        https://project-ghost-production.up.railway.app
      </div>
    </div>

    <div class="info-grid">
      <div class="info-card"><div class="info-card-title">Auth Required</div><div class="info-card-val">None</div><div class="info-card-sub">Open API — no key needed</div></div>
      <div class="info-card"><div class="info-card-title">Response Format</div><div class="info-card-val">JSON</div><div class="info-card-sub">All endpoints return JSON</div></div>
      <div class="info-card"><div class="info-card-title">Avg Response Time</div><div class="info-card-val">~7s</div><div class="info-card-sub">Including AI processing</div></div>
      <div class="info-card"><div class="info-card-title">MCP Support</div><div class="info-card-val">Native</div><div class="info-card-sub">Works with Cursor & Claude</div></div>
    </div>

    <div class="section-title" id="quickstart">Quick Start</div>
    <div class="section-sub">Make your first API call in 30 seconds.</div>
    <div class="code-block">
      <div class="code-tabs"><div class="code-tab active">cURL</div><div class="code-tab">Python</div><div class="code-tab">JavaScript</div></div>
      <div class="code-content"><span class="comment"># Extract structured data from any URL</span>
curl -X POST https://project-ghost-production.up.railway.app/distill \
  -H <span class="str">"Content-Type: application/json"</span> \
  -d <span class="str">'{"url": "https://apple.com"}'</span></div>
    </div>

    <div class="section-title">Endpoints</div>

    <div class="endpoint-block" id="distill">
      <div class="endpoint-header" onclick="toggle(this)">
        <span class="method-badge badge-post">POST</span>
        <span class="endpoint-path">/distill</span>
        <span class="endpoint-summary">Any URL → structured agent data</span>
        <span class="endpoint-chevron">▼</span>
      </div>
      <div class="endpoint-body open">
        <div class="ep-section">
          <div class="ep-label">Description</div>
          <div class="ep-desc">The core endpoint. Pass any public URL and receive structured intelligence — title, summary, entities, signals, confidence score, and tokens saved. Powers all agent use cases.</div>
        </div>
        <div class="ep-section">
          <div class="ep-label">Request Body</div>
          <div class="param-row"><span class="param-name">url <span class="param-req">required</span></span><span class="param-type">string</span><span class="param-desc">Any publicly accessible URL. Example: https://apple.com</span></div>
        </div>
        <div class="ep-section">
          <div class="ep-label">Example Request</div>
          <div class="code-block"><div class="code-content"><span class="comment">POST /distill</span>
{
  <span class="key">"url"</span>: <span class="str">"https://apple.com"</span>
}</div></div>
        </div>
        <div class="ep-section">
          <div class="ep-label">Example Response</div>
          <div class="response-badge"><span class="r-code">200</span><span class="r-desc">OK</span></div>
          <div class="code-block"><div class="code-content">{
  <span class="key">"url"</span>: <span class="str">"https://apple.com"</span>,
  <span class="key">"title"</span>: <span class="str">"Apple"</span>,
  <span class="key">"content"</span>: <span class="str">"Apple MacBook Neo Amazing Mac..."</span>,
  <span class="key">"tokens_saved"</span>: <span class="str">"91.2%"</span>,
  <span class="key">"signals_data"</span>: {
    <span class="key">"decision_signal"</span>: {
      <span class="key">"business_intent"</span>: <span class="str">"Apple promotes its latest hardware lineup..."</span>,
      <span class="key">"priority_score"</span>: <span class="num">8</span>,
      <span class="key">"category"</span>: <span class="str">"Technology"</span>
    },
    <span class="key">"items"</span>: [
      {
        <span class="key">"title"</span>: <span class="str">"MacBook Pro with M5"</span>,
        <span class="key">"entities"</span>: [<span class="str">"Apple"</span>, <span class="str">"MacBook"</span>, <span class="str">"M5"</span>],
        <span class="key">"impact_score"</span>: <span class="num">9</span>
      }
    ],
    <span class="key">"integrity_layer"</span>: {
      <span class="key">"confidence_score"</span>: <span class="num">0.87</span>,
      <span class="key">"is_high_integrity"</span>: <span class="num">true</span>
    }
  },
  <span class="key">"created_at"</span>: <span class="str">"2026-03-13T10:00:00Z"</span>
}</div></div>
        </div>
      </div>
    </div>

    <div class="endpoint-block" id="health">
      <div class="endpoint-header" onclick="toggle(this)">
        <span class="method-badge badge-get">GET</span>
        <span class="endpoint-path">/health</span>
        <span class="endpoint-summary">Liveness check</span>
        <span class="endpoint-chevron">▼</span>
      </div>
      <div class="endpoint-body">
        <div class="ep-section"><div class="ep-label">Description</div><div class="ep-desc">Returns API status and current version. Use this to verify the service is running before making distill calls.</div></div>
        <div class="ep-section">
          <div class="ep-label">Example Response</div>
          <div class="response-badge"><span class="r-code">200</span><span class="r-desc">OK</span></div>
          <div class="code-block"><div class="code-content">{
  <span class="key">"status"</span>: <span class="str">"ok"</span>,
  <span class="key">"version"</span>: <span class="str">"2.0"</span>
}</div></div>
        </div>
      </div>
    </div>

    <div class="endpoint-block" id="feed">
      <div class="endpoint-header" onclick="toggle(this)">
        <span class="method-badge badge-get">GET</span>
        <span class="endpoint-path">/feed</span>
        <span class="endpoint-summary">Poll cached intelligence signals</span>
        <span class="endpoint-chevron">▼</span>
      </div>
      <div class="endpoint-body">
        <div class="ep-section"><div class="ep-label">Description</div><div class="ep-desc">Returns previously processed signals from the Ghost intelligence database. Use this to poll for cached results without re-processing URLs.</div></div>
        <div class="ep-section">
          <div class="ep-label">Query Parameters</div>
          <div class="param-row"><span class="param-name">limit</span><span class="param-type">integer</span><span class="param-desc">Number of results to return. Default: 20, Max: 100</span></div>
          <div class="param-row"><span class="param-name">min_confidence</span><span class="param-type">float</span><span class="param-desc">Filter by minimum confidence score (0.0 - 1.0). Default: 0.5</span></div>
        </div>
        <div class="ep-section">
          <div class="ep-label">Example Request</div>
          <div class="code-block"><div class="code-content">GET /feed?limit=10&min_confidence=0.7</div></div>
        </div>
      </div>
    </div>

    <div class="endpoint-block" id="search">
      <div class="endpoint-header" onclick="toggle(this)">
        <span class="method-badge badge-get">GET</span>
        <span class="endpoint-path">/search</span>
        <span class="endpoint-summary">Search signals by entity name</span>
        <span class="endpoint-chevron">▼</span>
      </div>
      <div class="endpoint-body">
        <div class="ep-section"><div class="ep-label">Description</div><div class="ep-desc">Search the Ghost intelligence database by entity name. Returns all signals where the entity appears — companies, people, products.</div></div>
        <div class="ep-section">
          <div class="ep-label">Query Parameters</div>
          <div class="param-row"><span class="param-name">q <span class="param-req">required</span></span><span class="param-type">string</span><span class="param-desc">Entity name to search for. Example: nvidia, apple, elon musk</span></div>
          <div class="param-row"><span class="param-name">limit</span><span class="param-type">integer</span><span class="param-desc">Number of results. Default: 10</span></div>
        </div>
        <div class="ep-section">
          <div class="ep-label">Example Request</div>
          <div class="code-block"><div class="code-content">GET /search?q=apple&limit=5</div></div>
        </div>
      </div>
    </div>

    <div class="endpoint-block" id="mcp">
      <div class="endpoint-header" onclick="toggle(this)">
        <span class="method-badge badge-mcp">MCP</span>
        <span class="endpoint-path">/mcp</span>
        <span class="endpoint-summary">Native MCP agent integration</span>
        <span class="endpoint-chevron">▼</span>
      </div>
      <div class="endpoint-body">
        <div class="ep-section"><div class="ep-label">Description</div><div class="ep-desc">Native Model Context Protocol endpoint. Plug Ghost directly into Cursor, Claude Desktop, or any MCP-compatible agent framework as a tool.</div></div>
        <div class="ep-section">
          <div class="ep-label">MCP Config (Claude Desktop / Cursor)</div>
          <div class="code-block"><div class="code-content">{
  <span class="key">"mcpServers"</span>: {
    <span class="key">"project-ghost"</span>: {
      <span class="key">"url"</span>: <span class="str">"https://project-ghost-production.up.railway.app/mcp"</span>,
      <span class="key">"transport"</span>: <span class="str">"http"</span>
    }
  }
}</div></div>
        </div>
      </div>
    </div>

    <br><br>
    <p style="font-size:13px;color:#475569;padding-bottom:48px">Project Ghost v2.0 · <a href="https://project-ghost-lilac.vercel.app" style="color:#00e5a0">Landing Page</a> · <a href="mailto:ProjectGhost__@outlook.com" style="color:#00e5a0">Contact</a></p>
  </div>
</div>
<script>
function toggle(header) {
  const body = header.nextElementSibling;
  const chevron = header.querySelector('.endpoint-chevron');
  body.classList.toggle('open');
  chevron.style.transform = body.classList.contains('open') ? 'rotate(180deg)' : '';
}
function scrollTo(id) {
  document.getElementById(id)?.scrollIntoView({behavior:'smooth'});
}
</script>
</body>
</html>"""
        return HTMLResponse(html)

    mcp_app = mcp.http_app(path="/mcp")

    app = Starlette(routes=[
        Route("/", http_root, methods=["GET"]),
        Route("/distill", http_distill, methods=["POST"]),
        Route("/generate-key", http_generate_key, methods=["POST"]),
        Route("/health", http_health, methods=["GET"]),
        Route("/.well-known/mcp/server-card.json", http_server_card, methods=["GET"]),
        Mount("/mcp", app=mcp_app),
    ])
    from starlette.middleware.base import BaseHTTPMiddleware
    class SlashMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path == "/mcp":
                from starlette.datastructures import URL
                scope = dict(request.scope)
                scope["path"] = "/mcp/"
                scope["raw_path"] = b"/mcp/"
                request = request.__class__(scope, request._receive, request._send)
            return await call_next(request)
    app.add_middleware(SlashMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)