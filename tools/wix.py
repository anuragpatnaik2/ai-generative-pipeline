# tools/wix.py
# Python 3.8+ compatible — maps your collection fields:
# Title, Image, Date, Reporter Name, Subtitle, Article Text, Short Description, Full Name

import os
import json
import random
import re
from typing import Dict, Any, Optional

import httpx

import re

_SMARTS = {
    "\u2018": "'", "\u2019": "'",  # left/right single quotes
    "\u201C": '"', "\u201D": '"',  # left/right double quotes
    "\u201A": "'", "\u201B": "'", "\u201E": '"', "\u201F": '"',
    "\u2032": "'", "\u2035": "'", "\u2033": '"', "\u2036": '"',
}
_SMARTS_RE = re.compile("|".join(map(re.escape,_SMARTS.keys())))

def _dequote(s: str) -> str:
    if s is None: return ""
    # replace smart quotes
    s = _SMARTS_RE.sub(lambda m: _SMARTS[m.group(0)], str(s))
    # strip surrounding straight quotes and whitespace
    s = s.strip().strip('"').strip("'").strip()
    return s



WIX_API_KEY      = os.getenv("WIX_API_KEY", "")
WIX_SITE_ID      = os.getenv("WIX_SITE_ID", "")
WIX_COLLECTION_ID= os.getenv("WIX_COLLECTION_ID", "")
WIX_BASE         = os.getenv("WIX_API_BASE", "https://www.wixapis.com")

AUTHOR_CHOICES = [
    "Dahlia Arnold",
    "Novak Ivanovich",
    "Howard Lee",
    "Anurag Patnaik",
]
REPORTER_ORG = "AI Generative Org"  # fixed per your spec


class WixError(RuntimeError):
    ...


def _require_env() -> None:
    if not WIX_API_KEY or not WIX_SITE_ID or not WIX_COLLECTION_ID:
        raise WixError("WIX_API_KEY, WIX_SITE_ID, and WIX_COLLECTION_ID must be set")


def _headers() -> Dict[str,str]:
    key  = _dequote(os.getenv("WIX_API_KEY",""))
    site = _dequote(os.getenv("WIX_SITE_ID",""))
    if not key or not site:
        raise WixError("WIX_API_KEY and WIX_SITE_ID must be set")
    return {
        "Authorization": key,             # now ASCII-safe
        "wix-site-id": site,              # now ASCII-safe
        "Content-Type": "application/json; charset=utf-8",
    }


_SMARTS_RE = re.compile(r"[\u2018\u2019\u201A\u201B\u2032\u2035\u201C\u201D\u201E\u201F\u2033\u2036]")

def _normalize_text(s: Optional[str]) -> str:
    """
    Make text safe for JSON/HTML and UI:
    - coerce to str, trim
    - replace smart quotes with straight quotes
    - collapse whitespace
    - clamp to 60 chars only where the caller wants to
    """
    if s is None:
        return ""
    t = str(s).strip()
    # replace smart single/double quotes with straight equivalents
    t = _SMARTS_RE.sub(lambda m: "'" if m.group(0) in ("\u2018","\u2019","\u201A","\u201B","\u2032","\u2035")
                       else '"', t)
    # collapse whitespace
    t = re.sub(r"\s+", " ", t)
    return t


def _pick_full_name(seed: Optional[str] = None) -> str:
    rnd = random.Random(seed) if seed else random
    return rnd.choice(AUTHOR_CHOICES)


def build_payload(article: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a dict with your collection's exact field names:
      Title, Image, Date, Reporter Name, Subtitle, Article Text, Short Description, Full Name
    """
    title        = _normalize_text(article.get("approved_title") or article.get("title"))
    subtitle     = _normalize_text(article.get("subtitle"))
    short_desc   = _normalize_text(article.get("short_description"))[:160]
    body_html    = article.get("article_html") or ""   # allow HTML here
    image_url    = article.get("image_url") or ""
    date_iso     = article.get("published_at") or ""
    canonical    = article.get("canonical_url") or ""
    seed         = article.get("article_id") or article.get("id")
    full_name    = _pick_full_name(seed=seed)

    data = {
        "Title":              title,
        "Image":              {"src": image_url} if image_url else None,
        "Date":               date_iso,
        "Reporter Name":      REPORTER_ORG,
        "Subtitle":           subtitle,
        "Article Text":       body_html,
        "Short Description":  short_desc,
        "Full Name":          full_name,
        # optional: keep the source URL in a hidden/custom field if your collection has one
        # "Source URL":      canonical,
    }
    return data


async def create_or_update_item(doc: Dict[str, Any], existing_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Create or update a Wix CMS item (UTF-8 safe). `doc` must be the dict produced by build_payload().
    Returns the response dict (expects `item.id`).
    """
    _require_env()
    url_base = f"{WIX_BASE}/cms/v1/collections/{WIX_COLLECTION_ID}/items"

    async with httpx.AsyncClient(timeout=30.0) as client:
        if existing_id:
            # UPDATE
            url = f"{url_base}/{existing_id}"
            resp = await client.patch(url, headers=_headers(), json={"data": doc})
        else:
            # CREATE
            resp = await client.post(url_base, headers=_headers(), json={"data": doc})

    if resp.status_code >= 300:
        raise WixError(f"Wix API error {resp.status_code}: {resp.text}")

    try:
        data = resp.json()
    except Exception as e:
        raise WixError(f"Wix response JSON error: {e}; text={resp.text[:300]}")

    if "item" not in data:
        # Some tenants return at top-level; be defensive
        if "id" in data:
            return data
        raise WixError(f"Unexpected Wix response: {data}")

    return data