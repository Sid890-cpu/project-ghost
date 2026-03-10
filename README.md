# 👻 Project Ghost

> Web Intelligence Infrastructure for AI Agents

[![Status](https://img.shields.io/badge/status-live-00ff88?style=flat-square)](https://project-ghost-production.up.railway.app/health)
[![Version](https://img.shields.io/badge/version-2.0-00aaff?style=flat-square)](#)
[![MCP](https://img.shields.io/badge/MCP-compatible-ffaa00?style=flat-square)](#mcp-integration)

---

## What is Project Ghost?

AI agents waste compute reading messy HTML. Ghost converts any website into clean, structured intelligence signals — with entities, impact scores, and priority rankings — in under 4 seconds.

```
Any Website  →  Ghost  →  Structured JSON Signal
```

**Live API:** `https://project-ghost-production.up.railway.app`

---

## Quick Start

```bash
# Health check
curl https://project-ghost-production.up.railway.app/health

# Distill any URL
curl -X POST https://project-ghost-production.up.railway.app/distill \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{"url": "https://techcrunch.com"}'
```

---

## API Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| `GET` | `/health` | Liveness check | ❌ |
| `POST` | `/distill` | Distill any URL into signals | ✅ |
| `GET` | `/feed` | Poll cached signals | ✅ |
| `GET` | `/search?q=nvidia` | Search by entity | ✅ |
| `MCP` | `/mcp` | Agent tool integration | ✅ |

### Authentication

```
X-API-Key: your_api_key_here
```

---

## MCP Integration

Add Ghost to Cursor, Claude, or any MCP-compatible agent:

```json
{
  "mcpServers": {
    "project-ghost": {
      "url": "https://project-ghost-production.up.railway.app/mcp"
    }
  }
}
```

**Tools:** `distill_web` · `get_signal_feed` · `search_signals`

---

## Signal Schema

```json
{
  "decision_signal": {
    "business_intent": "string",
    "priority_score": 8.4,
    "category": "STARTUPS"
  },
  "items": [{
    "title": "string",
    "entities": ["Company", "Person"],
    "impact_score": 9.2,
    "summary": "string"
  }],
  "integrity_layer": {
    "confidence_score": 0.847,
    "is_high_integrity": true
  },
  "tokens_saved": "51.2%"
}
```

---

## Use Cases

- **Finance & Trading** — Detect market-moving events instantly
- **Sales Intelligence** — Trigger outreach on funding rounds and product launches
- **AI Agent Pipelines** — Real-time signal feed via MCP
- **Research** — Track emerging technologies automatically

---

## Trusted By

Findyble · Mocially · Terch DataLabs · ScaleGuide · BenchWatt

---

*Project Ghost v2.0 — For API access, contact via the live endpoint.*
