from __future__ import annotations
import os, json, httpx, re
from collections import Counter
from datetime import datetime
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
mcp = FastMCP(name="project-ghost")
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# Standard lightweight stop words to filter out "noise"
STOP_WORDS = {
    'the', 'and', 'this', 'that', 'with', 'from', 'news', 'more', 'your', 'their',
    'will', 'have', 'been', 'were', 'also', 'would', 'which', 'about', 'there'
}

def get_supabase():
    return create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_ANON_KEY"))

def get_dynamic_signals(text: str):
    """Automatically extracts only real-world entities (Proper Nouns)."""
    # 1. Regex to find only words that start with an UPPERCASE letter
    # This automatically ignores 'ago', 'hours', 'points', etc.
    proper_nouns = re.findall(r'\b[A-Z][a-z]{2,}\b', text)
    
    # 2. Filter out common "False Positives" (Words that start sentences but aren't entities)
    generic_start_words = {
        'The', 'And', 'This', 'That', 'With', 'From', 'News', 'More', 
        'Your', 'Their', 'Will', 'Have', 'Been', 'Were', 'Also', 'Would'
    }
    
    # 3. Clean the list: Must be 3+ letters and not a generic starter word
    refined_entities = [w for w in proper_nouns if w not in generic_start_words]
    
    # 4. Get the top 10 most frequent REAL entities
    most_common = dict(Counter(refined_entities).most_common(10))
    
    # 5. Density Score (Links) remains the same for structure checks
    density_score = len(re.findall(r'https?://', text))
    
    return most_common, density_score

async def get_hybrid_intelligence(text: str, raw_html: str):
    # 1. BASIC ENTITY COUNTING (Ensures no page has {})
    auto_counts, density = get_dynamic_signals(text)
    
    # 2. SIMPLE STRUCTURE CHECK
    soup_audit = BeautifulSoup(raw_html, "html.parser")
    # Count headers that look like news titles (length > 15)
    dom_detected_articles = len([h for h in soup_audit.find_all(['h1', 'h2', 'h3']) if len(h.text.strip()) > 15])
    # Fallback to a floor of 1 to avoid division by zero
    dom_detected_articles = max(dom_detected_articles, 1)

    # 3. AI EXTRACTION (Groq Llama 3.3 70B)
    prompt = f"Return ONLY JSON: {{'articles': [{{'title':str, 'sentiment':str, 'priority':int}}], 'detected_entities': list}}. Text: {text[:4000]}"
    
    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    ai_data = json.loads(completion.choices[0].message.content)
    
    # 4. SIMPLE SCORING MATH
    extracted_articles = len(ai_data.get("articles", []))
    
    # Rule 1: Structure Match = extracted / detected
    structure_match = min(extracted_articles / dom_detected_articles, 1.0)
    
    # Rule 2: Hallucination Penalty (0.0 if entities match, 0.4 if none match)
    ai_entities = [str(e).lower() for e in ai_data.get("detected_entities", [])]
    verified_entities = {e: auto_counts[e] for e in ai_entities if e in auto_counts}
    
    # Penalty is low if we found at least one verified entity
    hallucination_penalty = 0.0 if len(verified_entities) > 0 else 0.4

    # Rule 3: Simple Confidence
    # confidence = (structure_match * 0.6) + ((1 - hallucination_penalty) * 0.4)
    confidence = round((structure_match * 0.6) + ((1.0 - hallucination_penalty) * 0.4), 2)

    # 5. TRIGGER: Let pages pass integrity
    is_high_integrity = confidence > 0.5

    return {
        "articles": ai_data.get("articles", []),
        "verified_entities": verified_entities if verified_entities else auto_counts, # Never return empty
        "confidence_audit": {
            "score": confidence,
            "structure_match": round(structure_match, 2),
            "verified_count": len(verified_entities)
        },
        "triggers": {
            "is_high_integrity": is_high_integrity
        }
    }

@mcp.tool
async def distill_web(url: str):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await client.get(url)
    
    soup = BeautifulSoup(res.text, "html.parser")
    clean_text = " ".join(soup.get_text().split())
    
    # FIX: Pass BOTH clean_text and res.text (raw_html)
    signals = await get_hybrid_intelligence(clean_text, res.text)
    
    payload = {
        "url": url,
        "title": soup.title.string if soup.title else "No Title",
        "content": clean_text[:2000],
        "signals_data": signals,
        "tokens_saved": f"{round((1 - (len(clean_text) / len(res.text))) * 100, 1)}%",
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    
    get_supabase().table("ghost_memory").insert(payload).execute()
    return payload

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)