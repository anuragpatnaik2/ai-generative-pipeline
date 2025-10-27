import json, asyncio
from tools.news import load_config, canonical_url, sha, discover_feed_urls, parse_feed, search_newsapi, search_newscatcher, search_mediastack, score, within_freshness
import httpx
from datetime import datetime, timezone

async def get_top():
    cfg = load_config()
    core = ["openai.com","anthropic.com","ai.googleblog.com","ai.meta.com","microsoft.com","huggingface.co","stability.ai","blogs.nvidia.com","aws.amazon.com"]
    tier1= ["techcrunch.com","venturebeat.com","technologyreview.com","theverge.com","arstechnica.com","bloomberg.com"]
    items = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for f in cfg.get("feeds", []):
            feeds = await discover_feed_urls(client, f["homepage"])
            for fu in feeds[:3]:
                for it in parse_feed(fu):
                    it["url"] = canonical_url(it["url"])
                    items[sha(it["url"])] = it
        # backfills
        for func, key in [(search_newsapi,"newsapi"), (search_newscatcher,"newscatcher"), (search_mediastack,"mediastack")]:
            try:
                for it in (await func(client, cfg["apis"][key])):
                    items[sha(it["url"])] = it
            except Exception:
                pass
    lim = cfg["limits"]
    cand = [it for it in items.values()
            if len((it.get("title") or "")) >= lim["min_title_length"]
            and within_freshness(it, lim["freshness_hours"])]
    cand.sort(key=lambda it: (score(it, core, tier1), it.get("published_at") or ""), reverse=True)
    return cand[: max(8, lim["max_items_per_day"])]

if __name__=="__main__":
    top = asyncio.run(get_top())
    print(json.dumps(top, ensure_ascii=False, indent=2))
