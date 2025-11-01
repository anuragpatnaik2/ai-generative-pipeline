# tools/wix.py# tools/wix.py
# Python 3.8+ compatible

import os
import json
import random
from typing import Dict, Any, Optional

import httpx


# ENV you must set (locally and in App Runner):
#   WIX_API_KEY, WIX_SITE_ID, WIX_COLLECTION_ID
WIX_API_KEY: str = os.getenv("WIX_API_KEY", "")
WIX_SITE_ID: str = os.getenv("WIX_SITE_ID", "")
WIX_COLLECTION_ID: str = os.getenv("WIX_COLLECTION_ID", "")
WIX_BASE: str = os.getenv("WIX_API_BASE", "https://www.wixapis.com")

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
        raise WixError(
            "Missing one or more env vars: WIX_API_KEY, WIX_SITE_ID, WIX_COLLECTION_ID"
        )


def _headers() -> Dict[str, str]:
    return {
        "Authorization": WIX_API_KEY,
        "wix-site-id": WIX_SITE_ID,
        "Content-Type": "application/json; charset=utf-8",
    }


def _pick_full_name(seed: Optional[str] = None) -> str:
    """
    Pick a random full name. Optionally use a 'seed' (e.g., article_id)
    to keep the selection stable per article.
    """
    if seed:
        rnd = random.Random(seed)
        return rnd.choice(AUTHOR_CHOICES)
    return random.choice(AUTHOR_CHOICES)


def build_payload(article: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map your DynamoDB article to the Wix collection fields:
      Title, Image, Date, Reporter Name, Subtitle, Article Text,
      Short Description, Full Name
    """
    # Pull from your article item (Python dict already unmarshaled)
    title = article.get("approved_title") or article.get("title") or ""
    image_url = article.get("image_url") or ""
    date_iso = article.get("published_at") or ""  # ISO string
    subtitle = article.get("subtitle") or ""
    body_html = article.get("article_html") or ""  # send HTML in "Article Text"
    short_desc = article.get("short_description") or ""

    # Deterministic author pick per article (use article_id as seed if present)
    seed = article.get("article_id") or article.get("id")
    full_name = _pick_full_name(seed=seed)

    data = {
        "Title": title,
        "Image": {"src": image_url} if image_url else None,
        "Date": date_iso,
        "Reporter Name": REPORTER_ORG,
        "Subtitle": subtitle,
        "Article Text": body_html,
        "Short Description": short_desc[:160],  # keep meta/snippet tidy
        "Full Name": full_name,
    }

    # Wix CMS v1 expects {"data": {...}} as payload for item create/update
    return {"data": data}


async def create_or_update_item(doc: Dict[str, Any], existing_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Create or update a Wix CMS item in the target collection.
    Returns the parsed Wix response (which should include the item id).
    """
    _require_env()
    url_base = f"{WIX_BASE}/cms/v1/collections/{WIX_COLLECTION_ID}/items"

    async with httpx.AsyncClient(timeout=30.0) as client:
        if existing_id:
            # Update existing item
            url = f"{url_base}/{existing_id}"
            resp = await client.patch(url, headers=_headers(), content=json.dumps(doc))
        else:
            # Create new item
            resp = await client.post(url_base, headers=_headers(), content=json.dumps(doc))

    # Basic error handling
    if resp.status_code >= 300:
        raise WixError(f"Wix API error {resp.status_code}: {resp.text}")

    try:
        data = resp.json()
    except Exception as e:
        raise WixError(f"Wix response JSON error: {e}; text={resp.text[:300]}")

    # Typical response includes {"item": { "id": "..." , ...}}
    if "item" not in data:
        # Some responses may return the item root-level; be defensive
        if "id" in data:
            return data
        raise WixError(f"Unexpected Wix response: {data}")

    return data  # caller can extract item id via data["item"]["id"]
