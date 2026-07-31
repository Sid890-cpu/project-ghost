# 👻 Project Ghost

> Web Reading Layer for AI Agents

[![Status](https://img.shields.io/badge/status-live-00e5a0?style=flat-square)](https://project-ghost-rkwm.onrender.com/health)
[![Version](https://img.shields.io/badge/version-2.0-00aaff?style=flat-square)](#)
[![MCP](https://img.shields.io/badge/MCP-compatible-a78bfa?style=flat-square)](#mcp-integration)
[![Free Tier](https://img.shields.io/badge/free_tier-100_req%2Fmo-00e5a0?style=flat-square)](https://project-ghost-lilac.vercel.app)

---

## What is Project Ghost?

AI agents can't read the web properly. They get blocked, or drown in 50,000 tokens of raw HTML garbage.

Ghost sits between your agent and the web — converting any public URL into clean, structured intelligence in one API call.

```
Any URL  →  Ghost API  →  Structured JSON your agent can reason over
```

**Live API:** `https://project-ghost-rkwm.onrender.com`
**Landing Page:** `https://project-ghost-lilac.vercel.app`
**Agent Demo:** `https://project-ghost-lilac.vercel.app/ghost-demo.html`
**Integration Guide:** `https://project-ghost-lilac.vercel.app/integrate.html`

---

## Quick Start

```bash
# 1. Get a free API key at project-ghost-lilac.vercel.app
# 2. Call the API

curl -X POST https://project-ghost-rkwm.onrender.com/distill \
  -H "Authorization: Bearer ghost_sk_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://apple.com"}'
```

**Python:**
```python
import requests

response = requests.post(
    "https://project-ghost-rkwm.onrender.com/distill",
    headers={"Authorization": "Bearer ghost_sk_YOUR_KEY"},
    json={"url": "https://apple.com"}
)

data = response.json()
print(data["title"])
print(data["signals_data"]["decision_signal"]["business_intent"])
print(data["tokens_saved"])
```

---

## API Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| `GET` | `/health` | Liveness check | No |
| `POST` | `/distill` | Any URL to structured intelligence | Yes |
| `GET` | `/feed` | Poll cached signals | Yes |
| `GET` | `/search?q=nvidia` | Search by entity name | Yes |
| `MCP` | `/mcp` | Native MCP agent integration | Yes |

### Authentication

```
Authorization: Bearer ghost_sk_YOUR_KEY
```

Get a free key instantly at **project-ghost-lilac.vercel.app** — no credit card needed.

---

## Response Format

```json
{
  "url": "https://apple.com",
  "title": "Apple",
  "tokens_saved": "80.7%",
  "signals_data": {
    "decision_signal": {
      "business_intent": "Apple promotes its latest hardware lineup...",
      "priority_score": 8,
      "category": "Technology"
    },
    "items": [
      {
        "title": "MacBook Pro with M5",
        "entities": ["Apple", "MacBook", "M5"],
        "impact_score": 9
      }
    ],
    "integrity_layer": {
      "confidence_score": 0.87,
      "is_high_integrity": true
    }
  },
  "_usage": {
    "plan": "free",
    "requests_used": 1,
    "limit": 100,
    "remaining": 99
  }
}
```

---

## MCP Integration

Add Ghost directly to Cursor or Claude Desktop — no code needed:

```json
{
  "mcpServers": {
    "project-ghost": {
      "url": "https://project-ghost-rkwm.onrender.com/mcp",
      "transport": "http"
    }
  }
}
```

---

## Framework Integrations

### LangChain
```python
from langchain.tools import tool
import requests

@tool
def read_webpage(url: str) -> dict:
    """Read any public URL and extract structured intelligence."""
    res = requests.post(
        "https://project-ghost-rkwm.onrender.com/distill",
        headers={"Authorization": "Bearer ghost_sk_YOUR_KEY"},
        json={"url": url}
    ).json()
    return {
        "title": res.get("title"),
        "summary": res["signals_data"]["decision_signal"]["business_intent"],
        "entities": [e for i in res["signals_data"]["items"] for e in i["entities"]],
        "confidence": res["signals_data"]["integrity_layer"]["confidence_score"]
    }
```

### CrewAI
```python
from crewai_tools import BaseTool

class GhostWebReader(BaseTool):
    name: str = "Web Intelligence Reader"
    description: str = "Read any public URL and extract structured intelligence"

    def _run(self, url: str) -> str:
        res = requests.post(
            "https://project-ghost-rkwm.onrender.com/distill",
            headers={"Authorization": "Bearer ghost_sk_YOUR_KEY"},
            json={"url": url}
        ).json()
        return str(res["signals_data"])
```

Full integration examples at **project-ghost-lilac.vercel.app/integrate.html**

---

## Pricing

| Plan | Requests/month | Price |
|------|---------------|-------|
| Free | 100 | 0 |
| Developer | 2,000 | Rs 499 / ~$6 |
| Startup | 10,000 | Rs 1,999 / ~$24 |

---

## Use Cases

- **Research Agents** — Read any company website and extract structured intelligence
- **Sales & Outreach** — Detect buying signals from job postings and funding announcements
- **News Monitoring** — Track topics across multiple sources with entity extraction
- **Legal & Compliance** — Extract key terms from privacy policies and regulatory pages
- **Developer Tools** — Track GitHub trends and open source ecosystem

---

## Stack

- **Backend:** Python + FastMCP + Starlette + Uvicorn
- **AI:** Groq (Llama 3.3 70B)
- **Web Fetching:** Jina AI Reader
- **Database:** Supabase
- **Hosting:** Render.com (API) + Vercel (Frontend)

---

## Trusted By

Findyble · Mocially · Terch DataLabs · ScaleGuide · BenchWatt

---

## Links

- Landing Page: https://project-ghost-lilac.vercel.app
- Agent Demo: https://project-ghost-lilac.vercel.app/ghost-demo.html
- Integration Guide: https://project-ghost-lilac.vercel.app/integrate.html

---

*Project Ghost v2.0 — Web Intelligence for AI Agents*
