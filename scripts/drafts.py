import os, json, asyncio, re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from tools import storage
from tools.ai import generate_draft_fields
from tools.render import build_article_html, enforce_lengths
from tools.candidates import get_top

MAX_ITEMS = int(os.getenv("MAX_ITEMS_PER_DAY","6"))

def already_recent(item: dict) -> bool:
    # Simple guard: skip if we already have article_id present (published or draft)
    a = storage.get_article(item["article_id"])
    return a is not None

def to_item(candidate: dict, drafts: dict) -> dict:
    # Map candidate + drafts into DynamoDB item with your columns
    title = candidate["title"].strip()
    url = candidate["url"]
    fields = drafts
    errs = enforce_lengths(title, fields["subtitle"], fields["short_description"])
    # If summary too short/long, we still save as draft; you can edit later.
    html = build_article_html(title, fields["subtitle"], fields["short_description"],
                              fields["facts_bullets"], fields["why_bullets"], url)
    now = storage.now_iso()
    return {
        "article_id": candidate["article_id"],
        "canonical_url": url,
        "title": title,  # initial title (you'll pick approved later)
        "subtitle": fields["subtitle"],
        "short_description": fields["short_description"],
        "article_html": html,
        "image_url": candidate.get("image_url") or "",
        "reporter_name": candidate.get("author") or os.getenv("DEFAULT_REPORTER_NAME","AI-Generative News Desk"),
        "published_at": candidate.get("published_at") or now,
        "status": "awaiting_approval",
        "proposed_titles": fields["proposed_titles"],
        "tags": [], "slug": "", "meta_description": fields["short_description"],
        "run_id": os.getenv("RUN_ID","run_"+now[:10]),
        "created_at": now, "updated_at": now
    }

async def main():
    # 1) Fetch top candidates
    top = await get_top()
    # 2) Attach deterministic article IDs
    for c in top:
        c["article_id"] = storage.article_id_from_url(c["url"])
    # 3) Loop limited by MAX_ITEMS
    drafted = 0
    for c in top:
        if drafted >= MAX_ITEMS: break
        if already_recent(c): 
            continue
        # 4) Call OpenAI to produce fields
        fields = generate_draft_fields(c["title"], c["url"], snippet=None)
        # 5) Build item and save
        item = to_item(c, fields)
        storage.put_article(item)
        # 6) Save raw artifact for audit/debug
        storage.put_artifact(item["run_id"], f"articles/{item['article_id']}.json",
                             {"candidate": c, "draft_fields": fields})
        drafted += 1
        print(f"Drafted: {item['title']}  → article_id={item['article_id']}")
    if drafted == 0:
        print("No new drafts created (all duplicates or MAX_ITEMS=0).")

if __name__=="__main__":
    asyncio.run(main())
