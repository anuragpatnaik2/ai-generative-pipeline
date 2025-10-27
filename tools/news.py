import os, re, sys, time, json, hashlib
import httpx, feedparser, yaml
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from dotenv import load_dotenv; load_dotenv()

CONFIG_PATH = "config/sources.yaml"

def load_config():
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)

def canonical_url(url: str) -> str:
    # drop UTM & fragments
    u = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith("utm_")]
    u = u._replace(query=urlencode(q), fragment="")
    return urlunparse(u)

def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

async def discover_feed_urls(client: httpx.AsyncClient, homepage: str):
    urls = set()
    try:
        r = await client.get(homepage, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # <link rel="alternate" type="application/rss+xml" href="...">
        for link in soup.find_all("link", attrs={"rel": ["alternate", "ALTERNATE"]}):
            t = (link.get("type") or "").lower()
            if "rss" in t or "atom" in t or "xml" in t:
                href = link.get("href")
                if href and href.startswith(("http://","https://")):
                    urls.add(href)
        # common guesses
        for guess in ["/feed", "/rss", "/rss.xml", "/atom.xml", "/feeds/posts/default?alt=rss"]:
            try:
                url = homepage.rstrip("/") + guess
                rr = await client.head(url, timeout=5)
                if rr.status_code < 400:
                    urls.add(url)
            except Exception:
                pass
    except Exception:
        pass
    return list(urls)

def parse_feed(url: str):
    d = feedparser.parse(url)
    out = []
    for e in d.entries:
        title = (e.get("title") or "").strip()
        link = e.get("link")
        if not title or not link:
            continue
        published = None
        for key in ("published_parsed","updated_parsed"):
            if e.get(key):
                published = datetime(*e[key][:6], tzinfo=timezone.utc)
                break
        out.append({
            "title": title,
            "url": canonical_url(link),
            "published_at": published.isoformat() if published else None,
            "source": d.feed.get("title",""),
            "image_url": (e.get("media_content",[{}])[0].get("url") if e.get("media_content") else None),
            "author": (e.get("author") or ""),
        })
    return out

async def search_newsapi(client, cfg):
    key = os.getenv("NEWSAPI_KEY")
    if not key or not cfg.get("enabled"): return []
    params = {
        "q": cfg.get("query",""),
        "language": cfg.get("language","en"),
        "pageSize": cfg.get("pageSize", 25),
        "sortBy": "publishedAt",
    }
    domains = cfg.get("domains")
    if domains:
        params["domains"] = ",".join(domains)
    r = await client.get("https://newsapi.org/v2/everything", params=params, headers={"X-Api-Key": key}, timeout=15)
    r.raise_for_status()
    data = r.json()
    out = []
    for a in data.get("articles", []):
        out.append({
            "title": (a.get("title") or "").strip(),
            "url": canonical_url(a.get("url","")),
            "published_at": a.get("publishedAt"),
            "source": (a.get("source") or {}).get("name","NewsAPI"),
            "image_url": a.get("urlToImage"),
            "author": a.get("author") or "",
        })
    return out

async def search_newscatcher(client, cfg):
    key = os.getenv("NEWSCATCHER_KEY")
    if not key or not cfg.get("enabled"): return []
    params = {"q":"generative AI OR LLM", "lang":cfg.get("lang","en"), "sort_by":"date", "page_size":50}
    r = await client.get("https://v3-api.newscatcherapi.com/api/search", params=params,
                         headers={"x-api-token": key}, timeout=15)
    r.raise_for_status()
    data = r.json()
    out = []
    for a in data.get("articles", []):
        out.append({
            "title": (a.get("title") or "").strip(),
            "url": canonical_url(a.get("link","")),
            "published_at": a.get("published_date"),
            "source": a.get("clean_url") or "Newscatcher",
            "image_url": a.get("media"),
            "author": a.get("author") or "",
        })
    return out

async def search_mediastack(client, cfg):
    key = os.getenv("MEDIASTACK_KEY")
    if not key or not cfg.get("enabled"): return []
    params = {"access_key": key, "languages": cfg.get("languages","en"), "limit": 50,
              "keywords": "generative AI OR LLM", "categories": cfg.get("categories","technology")}
    r = await client.get("http://api.mediastack.com/v1/news", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    out = []
    for a in data.get("data", []):
        out.append({
            "title": (a.get("title") or "").strip(),
            "url": canonical_url(a.get("url","")),
            "published_at": a.get("published_at"),
            "source": a.get("source") or "mediastack",
            "image_url": a.get("image"),
            "author": a.get("author") or "",
        })
    return out

def score(item, core_domains, tier1_domains):
    title = (item.get("title") or "").lower()
    host = urlparse(item.get("url","")).hostname or ""
    s = 0
    if any(d in host for d in core_domains): s += 2
    if any(d in host for d in tier1_domains): s += 1
    if any(k in title for k in ["launch","model","sdk","api","pricing","fine-tune","roadmap","release"]): s += 1
    return s

def within_freshness(item, hours=72):
    ts = item.get("published_at")
    if not ts: return True
    try:
        dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
    except Exception:
        return True
    return (datetime.now(timezone.utc) - dt) <= timedelta(hours=hours)

async def main_preview():
    cfg = load_config()
    core = ["openai.com","anthropic.com","ai.googleblog.com","ai.meta.com","microsoft.com","huggingface.co","stability.ai","blogs.nvidia.com","aws.amazon.com"]
    tier1 = ["techcrunch.com","venturebeat.com","technologyreview.com","theverge.com","arstechnica.com","bloomberg.com"]

    items = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # RSS/Atom first
        for f in cfg.get("feeds", []):
            feeds = await discover_feed_urls(client, f["homepage"])
            if not feeds:
                continue
            for fu in feeds[:3]:  # keep it light
                for it in parse_feed(fu):
                    it["url"] = canonical_url(it["url"])
                    key = sha(it["url"])
                    items[key] = it

        # Backfills
        try:
            for it in (await search_newsapi(client, cfg["apis"]["newsapi"])):
                items[sha(it["url"])] = it
        except Exception:
            pass
        try:
            for it in (await search_newscatcher(client, cfg["apis"]["newscatcher"])):
                items[sha(it["url"])] = it
        except Exception:
            pass
        try:
            for it in (await search_mediastack(client, cfg["apis"]["mediastack"])):
                items[sha(it["url"])] = it
        except Exception:
            pass

    # Filter + score
    lim = cfg["limits"]
    cand = [it for it in items.values()
            if len((it.get("title") or "")) >= lim["min_title_length"]
            and within_freshness(it, lim["freshness_hours"])]
    cand.sort(key=lambda it: (score(it, core, tier1), it.get("published_at") or ""), reverse=True)

    top = cand[: max(8, lim["max_items_per_day"])]
    # Pretty print
    for i, it in enumerate(top, 1):
        print(f"{i}. {it.get('title')}")
        print(f"   {it.get('source')}  |  {it.get('published_at')} ")
        print(f"   {it.get('url')}")
        if it.get("image_url"):
            print(f"   image: {it['image_url']}")
        print()
    if not top:
        print("No candidates found. Check your network, keys, or widen freshness_hours.")
    return 0

if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main_preview()))
