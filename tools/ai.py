# tools/ai.py
import os, json
from typing import Dict, Any
from urllib.parse import urlparse

# Load .env from project root
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from openai import OpenAI
MODEL = os.getenv("OPENAI_MODEL","gpt-4o-mini")

# --- robust key loading + friendly error ---
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError(
        "OPENAI_API_KEY is not set. Put it in your project .env or export it in your shell."
    )

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def host_from_url(u:str)->str:
    try: return urlparse(u).hostname or ""
    except: return ""

def generate_draft_fields(title:str, url:str, snippet:str|None=None) -> Dict[str,Any]:
    """Returns dict with: short_description, subtitle, why_bullets[], facts_bullets[], proposed_titles[]"""
    with open("prompts/summarize.txt","r") as f: sum_prompt = f.read()
    with open("prompts/titles.txt","r") as f: titles_prompt = f.read()

    base = f"TITLE: {title}\nURL: {url}\nSNIPPET: {snippet or ''}\n"
    # 1) Summarization block (JSON-ish response)
    sum_resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role":"system","content":sum_prompt},
            {"role":"user","content": base + "\nReturn JSON with keys: SHORT_DESCRIPTION, SUBTITLE, WHY_IT_MATTERS(list), FACTS(list)."}
        ],
        temperature=0.5,
    )
    txt = sum_resp.choices[0].message.content.strip()
    # Try to find JSON
    import re, json
    m = re.search(r'\{.*\}', txt, re.S)
    data = json.loads(m.group(0)) if m else {"SHORT_DESCRIPTION":txt, "SUBTITLE": "", "WHY_IT_MATTERS":[], "FACTS":[]}

    # 2) Titles array
    tit_resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role":"system","content":titles_prompt},
            {"role":"user","content": f"TITLE: {title}\nURL: {url}"}
        ],
        temperature=0.6,
    )
    ttxt = tit_resp.choices[0].message.content.strip()
    try:
        proposed = json.loads(ttxt)
        if not isinstance(proposed, list): proposed=[str(ttxt)]
    except Exception:
        proposed = [ttxt]

    return {
        "short_description": (data.get("SHORT_DESCRIPTION") or "").strip()[:200],
        "subtitle": (data.get("SUBTITLE") or "").strip()[:140],
        "why_bullets": [b.strip() for b in data.get("WHY_IT_MATTERS",[]) if b.strip()][:5],
        "facts_bullets": [b.strip() for b in data.get("FACTS",[]) if b.strip()][:8],
        "proposed_titles": [s.strip()[:80] for s in proposed if s and isinstance(s,str)][:3],
        "source_host": host_from_url(url)
    }
