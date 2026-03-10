from __future__ import annotations
import os, json, httpx, re, random, time
import xml.etree.ElementTree as ET
from datetime import datetime
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route, Mount
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
import uvicorn

load_dotenv()
mcp = FastMCP(name="project-ghost")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# ── API key validation ────────────────────────────────────────────────────────
# Set GHOST_API_KEY in Railway env vars. If not set, API is open (dev mode).
GHOST_API_KEY = os.environ.get("GHOST_API_KEY", "")

def check_api_key(request: Request) -> bool:
    if not GHOST_API_KEY:
        return True  # open in dev mode
    key = request.headers.get("X-API-Key", "") or request.query_params.get("api_key", "")
    return key == GHOST_API_KEY

# ── Request timing middleware ─────────────────────────────────────────────────
class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        ms = round((time.perf_counter() - start) * 1000)
        response.headers["X-Response-Time"] = f"{ms}ms"
        response.headers["X-Powered-By"] = "Project Ghost"
        return response

# --- Rotation pool to reduce blocking ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Sites known to hard-block scrapers → use their RSS/JSON feeds instead
RSS_OVERRIDES = {
    "www.bloomberg.com":           "https://feeds.bloomberg.com/markets/news.rss",
    "www.wsj.com":                 "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "www.reuters.com":             "https://www.reutersagency.com/feed/?best-topics=tech&post_type=best",
    "www.cnbc.com":                "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "techcrunch.com":              "https://techcrunch.com/feed/",
    "www.theverge.com":            "https://www.theverge.com/rss/index.xml",
    "news.ycombinator.com":        "https://news.ycombinator.com/rss",
    "www.bbc.com":                 "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "timesofindia.indiatimes.com": "https://timesofindia.indiatimes.com/rssfeeds/5880811.cms",
    "www.reddit.com":              "https://www.reddit.com/r/technology/top/.json?limit=25&t=day",
}

def get_supabase():
    return create_client(
        os.environ.get("SUPABASE_URL"),
        os.environ.get("SUPABASE_ANON_KEY")
    )

def is_blocked(text: str, status: int) -> bool:
    blocked_signals = ["robot", "denied", "access denied", "403 forbidden",
                       "enable javascript", "please verify", "captcha", "cloudflare"]
    if status not in (200, 201, 301, 302):
        return True
    low = text.lower()
    return any(sig in low for sig in blocked_signals) and len(text) < 5000


def parse_rss_to_text(xml_text: str, max_items: int = 15) -> tuple[str, str]:
    """
    Parse RSS/Atom XML and return (clean_headlines_text, feed_title).
    Extracts only title + description per item — no XML noise sent to Groq.
    """
    try:
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        # Detect Atom vs RSS
        is_atom = root.tag.endswith("feed") or "atom" in root.tag.lower()

        headlines = []
        feed_title = "Unknown Feed"

        if is_atom:
            title_el = root.find("atom:title", ns) or root.find("title")
            if title_el is not None and title_el.text:
                feed_title = title_el.text.strip()
            for entry in list(root.findall("atom:entry", ns) or root.findall("entry"))[:max_items]:
                t = entry.find("atom:title", ns) or entry.find("title")
                s = entry.find("atom:summary", ns) or entry.find("summary") or entry.find("content")
                parts = []
                if t is not None and t.text:
                    parts.append(t.text.strip())
                if s is not None and s.text:
                    parts.append(re.sub(r"<[^>]+>", "", s.text).strip()[:200])
                if parts:
                    headlines.append(" — ".join(parts))
        else:
            # Standard RSS 2.0
            channel = root.find("channel")
            if channel is not None:
                ct = channel.find("title")
                if ct is not None and ct.text:
                    feed_title = ct.text.strip()
                for item in list(channel.findall("item"))[:max_items]:
                    t = item.find("title")
                    d = item.find("description")
                    parts = []
                    if t is not None and t.text:
                        parts.append(t.text.strip())
                    if d is not None and d.text:
                        clean_desc = re.sub(r"<[^>]+>", "", d.text).strip()[:200]
                        if clean_desc:
                            parts.append(clean_desc)
                    if parts:
                        headlines.append(" — ".join(parts))

        if not headlines:
            return None, feed_title

        clean_text = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))
        return clean_text, feed_title

    except ET.ParseError:
        return None, "Unknown Feed"


async def fetch_url(url: str) -> tuple[str | None, str]:
    """Fetch URL. Uses RSS override for known-blocked domains and parses XML properly."""
    domain = url.split("//")[-1].split("/")[0]
    fetch_target = RSS_OVERRIDES.get(domain, url)
    using_rss = fetch_target != url

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    try:
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=True, timeout=30.0
        ) as client:
            res = await client.get(fetch_target)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"  ↳ Network error fetching {fetch_target}: {e}")
        return None, domain

    # ── Reddit JSON ──────────────────────────────────────────────────────────
    if fetch_target.endswith(".json"):
        try:
            reddit_data = json.loads(res.text)
            posts = reddit_data["data"]["children"]
            text = "\n".join(
                f"{i+1}. {p['data']['title']}" for i, p in enumerate(posts[:20])
            )
            return text, "Reddit r/technology"
        except Exception:
            return None, domain

    # ── RSS / Atom XML ───────────────────────────────────────────────────────
    content_type = res.headers.get("content-type", "")
    is_xml = (
        using_rss
        or "xml" in content_type
        or "rss" in content_type
        or res.text.strip().startswith("<?xml")
        or res.text.strip().startswith("<rss")
        or res.text.strip().startswith("<feed")
    )

    if is_xml:
        clean_text, feed_title = parse_rss_to_text(res.text)
        return clean_text, feed_title

    # ── Regular HTML page ────────────────────────────────────────────────────
    if is_blocked(res.text, res.status_code):
        return None, domain

    soup = BeautifulSoup(res.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside", "iframe", "noscript"]):
        tag.decompose()

    clean_text = " ".join(soup.get_text(separator=" ").split())
    title = soup.title.string.strip() if soup.title else domain
    return clean_text, title


async def get_hybrid_intelligence(text: str, source_url: str) -> dict:
    """Run Groq LLM to extract structured signals from raw text."""

    capped_text = text[:3000]
    now_iso = datetime.utcnow().isoformat() + "Z"

    # Category weights for priority calculation (server-side only, never from LLM)
    CATEGORY_WEIGHTS = {
        "FINANCE":     1.0,
        "MARKETS":     1.0,
        "GEOPOLITICS": 0.95,
        "STARTUPS":    0.85,
        "AI":          0.90,
        "TECH":        0.80,
        "GENERAL":     0.60,
    }

    system_prompt = (
        "You are a financial intelligence analyst extracting signals for AI agents and B2B clients.\n"
        "Extract exactly 3 business-critical signals from the content below.\n\n"
        "Return ONLY a JSON object with this exact structure:\n"
        "{\n"
        '  "business_intent": "<1-2 sentences: what trend or risk this source signals to a business reader>",\n'
        '  "category": "<one of: FINANCE|TECH|GEOPOLITICS|MARKETS|STARTUPS|AI|GENERAL>",\n'
        '  "items": [\n'
        '    {\n'
        '      "title": "<exact headline from content>",\n'
        f'     "published_time": "{now_iso}",\n'
        '      "entities": ["<specific company name>", "<specific person or product>", "<country or market if relevant>"],\n'
        '      "impact_score": <integer 1-10: 9-10=market-moving event, 7-8=significant business impact, 4-6=moderate relevance, 1-3=low signal>,\n'
        '      "summary": "<15-25 word sentence stating WHO did WHAT and WHY it matters to businesses>"\n'
        '    }\n'
        "  ]\n"
        "}\n\n"
        "STRICT RULES:\n"
        "- summary MUST be 15-25 words stating a specific fact, not a label. BAD: 'Oil price surges'. GOOD: 'Oil climbs above $100 as Saudi Arabia cuts production amid Iran conflict, raising energy costs for manufacturers.'\n"
        "- entities MUST be specific named companies, people, or products. BAD: ['US', 'Wall Street', 'WTI']. GOOD: ['Saudi Aramco', 'OPEC', 'Brent Crude'].\n"
        f"- published_time MUST always be exactly: {now_iso}\n"
        "- No markdown. No explanation outside the JSON. No null values. Exactly 3 items.\n"
        "- Do NOT include a priority_score field."
    )

    user_prompt = f"Source: {source_url}\n\nContent:\n{capped_text}"

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1000,  # richer summaries need more room
        )

        raw_content = completion.choices[0].message.content.strip()
        raw_content = re.sub(r"^```json|^```|```$", "", raw_content, flags=re.MULTILINE).strip()
        ai_raw = json.loads(raw_content)

        items = ai_raw.get("items", [])
        if not isinstance(items, list):
            items = []

        category = ai_raw.get("category", "GENERAL")

        # ── Dynamic priority_score (server-side, never from LLM) ────────────
        # Formula: weighted average of impact scores
        #          × entity richness bonus (more named entities = higher signal)
        #          × category weight
        #          scaled to 1–10
        if items:
            avg_impact    = sum(float(i.get("impact_score", 5)) for i in items) / len(items)
            entity_counts = [len(i.get("entities", [])) for i in items]
            avg_entities  = sum(entity_counts) / len(entity_counts)
            # Entity bonus: 0 entities=0.8x, 1=0.9x, 2=1.0x, 3+=1.1x
            entity_bonus  = min(0.8 + (avg_entities * 0.1), 1.15)
            cat_weight    = CATEGORY_WEIGHTS.get(category, 0.70)
            raw_priority  = avg_impact * entity_bonus * cat_weight
            # Clamp to 1.0–10.0 and round to 1 decimal
            priority_score = round(max(1.0, min(raw_priority, 10.0)), 1)
        else:
            priority_score = 1.0

        # ── Confidence score ─────────────────────────────────────────────────
        avg_entities_flat = (
            sum(len(i.get("entities", [])) for i in items) / len(items) if items else 0
        )
        avg_impact_flat = (
            sum(float(i.get("impact_score", 0)) for i in items) / len(items) if items else 0
        )
        confidence = round(
            min((len(items) * 0.15) + (avg_entities_flat * 0.08) + (avg_impact_flat * 0.035) + 0.2, 1.0), 3
        )

        return {
            "decision_signal": {
                "business_intent": ai_raw.get("business_intent", ""),
                "priority_score":  priority_score,
                "category":        category,
            },
            "items": items,
            "integrity_layer": {
                "confidence_score": confidence,
                "is_high_integrity": confidence > 0.7,
                "items_extracted":   len(items),
                "avg_impact_score":  round(avg_impact_flat, 2),
            },
        }

    except Exception as e:
        return {
            "decision_signal": {},
            "items": [],
            "integrity_layer": {
                "confidence_score": 0,
                "is_high_integrity": False,
                "error": str(e),
            },
        }


@mcp.tool
async def distill_web(url: str) -> dict:
    """
    Distill a URL into structured intelligence signals for AI agents.
    Returns a Ghost Signal payload with decision_signal, items, and integrity_layer.
    """
    domain = url.split("//")[-1].split("/")[0]
    clean_text, title = await fetch_url(url)

    if clean_text is None:
        return {
            "url": url,
            "title": f"Blocked — {domain}",
            "content": "",
            "signals_data": {
                "decision_signal": {},
                "items": [],
                "integrity_layer": {"confidence_score": 0, "is_high_integrity": False},
            },
            "tokens_saved": "0%",
            "created_at": datetime.utcnow().isoformat() + "Z",
        }

    raw_char_count   = len(clean_text)
    groq_char_count  = min(raw_char_count, 3000)
    tokens_saved_pct = f"{round((1 - groq_char_count / max(raw_char_count, 1)) * 100, 1)}%"
    signals          = await get_hybrid_intelligence(clean_text, url)

    ds    = signals.get("decision_signal", {})
    il    = signals.get("integrity_layer", {})
    items = signals.get("items", [])
    all_entities = list({e for item in items for e in item.get("entities", [])})

    payload = {
        "url":          url,
        "title":        title,
        "content":      clean_text[:2000],
        "signals_data": signals,
        "tokens_saved": tokens_saved_pct,
        "created_at":   datetime.utcnow().isoformat() + "Z",
        "confidence":   il.get("confidence_score", 0),
        "entities":     ", ".join(all_entities[:10]),
        "intent":       ds.get("business_intent", ""),
        "summary":      items[0].get("summary", "") if items else "",
    }

    try:
        get_supabase().table("ghost_memory").insert(payload).execute()
    except Exception as e:
        print(f"⚠️ Supabase insert error: {e}")

    return payload


@mcp.tool
async def get_signal_feed(limit: int = 20, min_confidence: float = 0.5) -> dict:
    """Retrieve the latest Ghost signals from memory."""
    try:
        result = (
            get_supabase()
            .table("ghost_memory")
            .select("url, title, intent, summary, confidence, entities, signals_data, created_at")
            .gte("confidence", min_confidence)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        return {
            "feed_version": "1.0",
            "fetched_at":   datetime.utcnow().isoformat() + "Z",
            "total":        len(rows),
            "signals":      rows,
        }
    except Exception as e:
        return {"error": str(e), "signals": []}


@mcp.tool
async def search_signals(query: str, limit: int = 10) -> dict:
    """Search ghost_memory for signals matching a keyword or entity."""
    try:
        result = (
            get_supabase()
            .table("ghost_memory")
            .select("url, title, intent, summary, confidence, entities, created_at")
            .ilike("entities", f"%{query}%")
            .order("confidence", desc=True)
            .limit(limit)
            .execute()
        )
        return {
            "query":   query,
            "total":   len(result.data or []),
            "signals": result.data or [],
        }
    except Exception as e:
        return {"error": str(e), "signals": []}


# ═══════════════════════════════════════════════════════════════════════════════
#  REST API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

def api_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"success": False, "error": message}, status_code=status)

def api_ok(data: dict, meta: dict | None = None) -> JSONResponse:
    response = {"success": True, **data}
    if meta:
        response["meta"] = meta
    return JSONResponse(response)


async def route_health(request: Request) -> JSONResponse:
    """GET /health — liveness check, no auth required"""
    return JSONResponse({
        "status":    "online",
        "service":   "project-ghost",
        "version":   "2.0",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


async def route_distill(request: Request) -> JSONResponse:
    """
    POST /distill
    Body: { "url": "https://techcrunch.com" }
    Returns structured intelligence signal for the given URL.
    """
    if not check_api_key(request):
        return api_error("Invalid or missing API key. Pass X-API-Key header.", 401)

    try:
        body = await request.json()
    except Exception:
        return api_error("Request body must be valid JSON with a 'url' field.")

    url = body.get("url", "").strip()
    if not url:
        return api_error("Missing required field: 'url'")
    if not url.startswith("http"):
        return api_error("Invalid URL — must start with http:// or https://")

    t0     = time.perf_counter()
    result = await distill_web(url)
    ms     = round((time.perf_counter() - t0) * 1000)

    return api_ok(
        {"signal": result},
        meta={"processing_time_ms": ms, "url": url}
    )


async def route_feed(request: Request) -> JSONResponse:
    """
    GET /feed?limit=20&min_confidence=0.5
    Returns the latest cached signals from Supabase — no re-scraping.
    """
    if not check_api_key(request):
        return api_error("Invalid or missing API key. Pass X-API-Key header.", 401)

    try:
        limit          = int(request.query_params.get("limit", 20))
        min_confidence = float(request.query_params.get("min_confidence", 0.5))
        limit          = max(1, min(limit, 100))
        min_confidence = max(0.0, min(min_confidence, 1.0))
    except ValueError:
        return api_error("Invalid query params — limit must be int, min_confidence must be float.")

    result = await get_signal_feed(limit=limit, min_confidence=min_confidence)
    return api_ok(result)


async def route_search(request: Request) -> JSONResponse:
    """
    GET /search?q=nvidia&limit=10
    Search cached signals by entity, company, or keyword.
    """
    if not check_api_key(request):
        return api_error("Invalid or missing API key. Pass X-API-Key header.", 401)

    query = request.query_params.get("q", "").strip()
    if not query:
        return api_error("Missing required query param: 'q'")

    try:
        limit = int(request.query_params.get("limit", 10))
        limit = max(1, min(limit, 50))
    except ValueError:
        return api_error("Invalid param — limit must be an integer.")

    result = await search_signals(query=query, limit=limit)
    return api_ok(result)


async def route_docs(request: Request) -> HTMLResponse:
    """GET / — API documentation page"""
    base_url = "https://project-ghost-production.up.railway.app"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Project Ghost — API Docs</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ background: #080808; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 40px 20px; }}
    .container {{ max-width: 860px; margin: 0 auto; }}
    .logo {{ font-size: 28px; font-weight: bold; color: #00ff88; letter-spacing: 4px; margin-bottom: 6px; }}
    .tagline {{ color: #666; font-size: 13px; margin-bottom: 48px; }}
    .badge {{ display: inline-block; background: #00ff8822; color: #00ff88; border: 1px solid #00ff8844; padding: 2px 10px; border-radius: 20px; font-size: 11px; margin-left: 10px; vertical-align: middle; }}
    h2 {{ color: #00ff88; font-size: 11px; letter-spacing: 3px; text-transform: uppercase; margin-bottom: 20px; margin-top: 48px; border-bottom: 1px solid #1a1a1a; padding-bottom: 10px; }}
    .endpoint {{ background: #0f0f0f; border: 1px solid #1e1e1e; border-radius: 8px; margin-bottom: 16px; overflow: hidden; }}
    .endpoint-header {{ padding: 16px 20px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #1a1a1a; }}
    .method {{ font-size: 11px; font-weight: bold; padding: 3px 10px; border-radius: 4px; letter-spacing: 1px; }}
    .get  {{ background: #003d1f; color: #00ff88; }}
    .post {{ background: #1a2a00; color: #aaff00; }}
    .path {{ color: #fff; font-size: 14px; }}
    .desc {{ color: #666; font-size: 12px; padding: 14px 20px; }}
    .params {{ padding: 0 20px 16px; }}
    .param {{ display: flex; gap: 16px; padding: 8px 0; border-bottom: 1px solid #111; font-size: 12px; }}
    .param:last-child {{ border-bottom: none; }}
    .param-name {{ color: #00aaff; min-width: 130px; }}
    .param-type {{ color: #888; min-width: 60px; }}
    .param-desc {{ color: #aaa; }}
    .code-block {{ background: #050505; border: 1px solid #1a1a1a; border-radius: 6px; padding: 16px 20px; margin: 12px 20px 20px; overflow-x: auto; }}
    .code-block pre {{ font-size: 12px; line-height: 1.6; color: #ccc; white-space: pre; }}
    .key {{ color: #00aaff; }}
    .str {{ color: #00ff88; }}
    .num {{ color: #ffaa00; }}
    .bool {{ color: #ff6688; }}
    .label {{ font-size: 10px; color: #444; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 8px; }}
    .auth-box {{ background: #0f0f0f; border: 1px solid #2a1a00; border-radius: 8px; padding: 20px; margin-bottom: 16px; }}
    .auth-box code {{ background: #1a1a1a; padding: 2px 8px; border-radius: 4px; color: #ffaa00; font-size: 12px; }}
    .status {{ display: inline-flex; align-items: center; gap: 6px; }}
    .dot {{ width: 8px; height: 8px; background: #00ff88; border-radius: 50%; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:0.3 }} }}
    footer {{ margin-top: 64px; padding-top: 24px; border-top: 1px solid #1a1a1a; color: #333; font-size: 11px; }}
  </style>
</head>
<body>
<div class="container">

  <div class="logo">👻 PROJECT GHOST <span class="badge">v2.0</span></div>
  <div class="tagline">Web Intelligence Infrastructure for AI Agents &nbsp;·&nbsp; <span class="status"><span class="dot"></span> API Online</span></div>

  <h2>Authentication</h2>
  <div class="auth-box">
    Pass your API key in the request header:<br><br>
    <code>X-API-Key: your_api_key_here</code>
    <br><br>
    Or as a query param: <code>?api_key=your_api_key_here</code>
  </div>

  <h2>Base URL</h2>
  <div class="auth-box">
    <code>{base_url}</code>
  </div>

  <h2>Endpoints</h2>

  <!-- /health -->
  <div class="endpoint">
    <div class="endpoint-header">
      <span class="method get">GET</span>
      <span class="path">/health</span>
    </div>
    <div class="desc">Liveness check. No authentication required.</div>
    <div class="code-block">
      <div class="label">Response</div>
      <pre><span class="key">"status"</span>:    <span class="str">"online"</span>,
<span class="key">"service"</span>:   <span class="str">"project-ghost"</span>,
<span class="key">"version"</span>:   <span class="str">"2.0"</span>,
<span class="key">"timestamp"</span>: <span class="str">"2026-03-09T12:00:00Z"</span></pre>
    </div>
  </div>

  <!-- /distill -->
  <div class="endpoint">
    <div class="endpoint-header">
      <span class="method post">POST</span>
      <span class="path">/distill</span>
    </div>
    <div class="desc">Distill any URL into structured intelligence signals in real-time. Scrapes, cleans, and runs AI extraction on the content.</div>
    <div class="params">
      <div class="label" style="margin-top:8px">Body (JSON)</div>
      <div class="param"><span class="param-name">url</span><span class="param-type">string*</span><span class="param-desc">The URL to distill. Works on any public webpage or RSS feed.</span></div>
    </div>
    <div class="code-block">
      <div class="label">Request</div>
      <pre>curl -X POST {base_url}/distill \\
  -H <span class="str">"X-API-Key: your_key"</span> \\
  -H <span class="str">"Content-Type: application/json"</span> \\
  -d '&#123;<span class="str">"url"</span>: <span class="str">"https://techcrunch.com"</span>&#125;'</pre>
    </div>
    <div class="code-block">
      <div class="label">Response</div>
      <pre>&#123;
  <span class="key">"success"</span>: <span class="bool">true</span>,
  <span class="key">"signal"</span>: &#123;
    <span class="key">"title"</span>:        <span class="str">"TechCrunch"</span>,
    <span class="key">"tokens_saved"</span>: <span class="str">"51.2%"</span>,
    <span class="key">"confidence"</span>:   <span class="num">0.847</span>,
    <span class="key">"decision_signal"</span>: &#123;
      <span class="key">"business_intent"</span>: <span class="str">"AI funding activity is accelerating..."</span>,
      <span class="key">"priority_score"</span>:  <span class="num">7.4</span>,
      <span class="key">"category"</span>:        <span class="str">"STARTUPS"</span>
    &#125;,
    <span class="key">"items"</span>: [ ... ]
  &#125;,
  <span class="key">"meta"</span>: &#123; <span class="key">"processing_time_ms"</span>: <span class="num">2340</span> &#125;
&#125;</pre>
    </div>
  </div>

  <!-- /feed -->
  <div class="endpoint">
    <div class="endpoint-header">
      <span class="method get">GET</span>
      <span class="path">/feed</span>
    </div>
    <div class="desc">Poll the cached signal feed from Supabase. Returns pre-processed signals — no scraping delay.</div>
    <div class="params">
      <div class="label" style="margin-top:8px">Query Params</div>
      <div class="param"><span class="param-name">limit</span><span class="param-type">int</span><span class="param-desc">Number of signals to return. Default: 20, max: 100.</span></div>
      <div class="param"><span class="param-name">min_confidence</span><span class="param-type">float</span><span class="param-desc">Minimum confidence threshold (0.0–1.0). Default: 0.5.</span></div>
    </div>
    <div class="code-block">
      <div class="label">Request</div>
      <pre>curl "{base_url}/feed?limit=10&min_confidence=0.7" \\
  -H <span class="str">"X-API-Key: your_key"</span></pre>
    </div>
  </div>

  <!-- /search -->
  <div class="endpoint">
    <div class="endpoint-header">
      <span class="method get">GET</span>
      <span class="path">/search</span>
    </div>
    <div class="desc">Search the signal memory by entity, company name, or keyword.</div>
    <div class="params">
      <div class="label" style="margin-top:8px">Query Params</div>
      <div class="param"><span class="param-name">q</span><span class="param-type">string*</span><span class="param-desc">Search term. Matches against extracted entities (e.g. "Nvidia", "OpenAI").</span></div>
      <div class="param"><span class="param-name">limit</span><span class="param-type">int</span><span class="param-desc">Max results. Default: 10, max: 50.</span></div>
    </div>
    <div class="code-block">
      <div class="label">Request</div>
      <pre>curl "{base_url}/search?q=nvidia&limit=5" \\
  -H <span class="str">"X-API-Key: your_key"</span></pre>
    </div>
  </div>

  <h2>MCP Integration (for AI Agents)</h2>
  <div class="auth-box">
    Add Ghost directly to Cursor, Claude, or any MCP-compatible agent:<br><br>
    <code>{base_url}/mcp</code><br><br>
    Available tools: <code>distill_web</code> &nbsp;·&nbsp; <code>get_signal_feed</code> &nbsp;·&nbsp; <code>search_signals</code>
  </div>

  <footer>Project Ghost &nbsp;·&nbsp; Signal Engine v2.0 &nbsp;·&nbsp; Built for AI agents &nbsp;·&nbsp; Powered by Groq + Supabase</footer>
</div>
</body>
</html>"""
    return HTMLResponse(html)


# ═══════════════════════════════════════════════════════════════════════════════
#  APP ASSEMBLY — MCP + REST on the same server
# ═══════════════════════════════════════════════════════════════════════════════

from starlette.routing import Mount

# path="/" because we mount the app at /mcp below — paths don't double up
mcp_asgi = mcp.http_app(path="/")

# Starlette gets MCP's lifespan so its session manager initializes correctly
app = Starlette(
    lifespan=mcp_asgi.lifespan,
    routes=[
        # REST routes
        Route("/",        route_docs),
        Route("/health",  route_health),
        Route("/distill", route_distill, methods=["POST"]),
        Route("/feed",    route_feed,    methods=["GET"]),
        Route("/search",  route_search,  methods=["GET"]),
        # MCP mount — tools available at /mcp
        Mount("/mcp", app=mcp_asgi),
    ]
)
app.add_middleware(TimingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"👻 Project Ghost running on port {port}")
    print(f"   REST API : http://0.0.0.0:{port}/")
    print(f"   MCP      : http://0.0.0.0:{port}/mcp")
    uvicorn.run(app, host="0.0.0.0", port=port)