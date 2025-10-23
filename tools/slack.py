# tools/slack.py
import os
import hmac
import hashlib
import time
import json
from typing import Dict, Optional

import httpx


SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET: str = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_CHANNEL_ID: str = os.getenv("SLACK_CHANNEL_ID", "")  # optional default


class SlackError(RuntimeError):
    ...


def _get_header(headers: Dict[str, str], name: str) -> Optional[str]:
    """
    Retrieve a header in a case-insensitive way from Starlette/FastAPI Request.headers.
    """
    return headers.get(name) or headers.get(name.lower()) or headers.get(name.upper())


def verify_signature(headers: Dict[str, str], body: bytes) -> None:
    """
    Validate Slack request signature (v0 scheme). Raises SlackError on failure.
    See: https://api.slack.com/authentication/verifying-requests-from-slack
    """
    if not SLACK_SIGNING_SECRET:
        raise SlackError("Signing secret not configured")

    ts = _get_header(headers, "X-Slack-Request-Timestamp")
    sig = _get_header(headers, "X-Slack-Signature")
    if not ts or not sig:
        raise SlackError(f"Missing Slack signature headers (ts={bool(ts)} sig={bool(sig)})")

    try:
        ts_int = int(ts)
    except Exception:
        raise SlackError(f"Bad timestamp header: {ts!r}")

    # Prevent replay attacks: reject requests older/newer than 5 minutes.
    if abs(time.time() - ts_int) > 60 * 5:
        raise SlackError(f"Stale timestamp: {ts_int}")

    # Slack signs "v0:{ts}:{raw_body}"
    base = b"v0:%d:" % ts_int
    raw = body or b""
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        base + raw,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, sig):
        # Do NOT log secrets or full body
        raise SlackError("Signature mismatch")


async def post_message(blocks: Dict, channel: Optional[str] = None) -> Dict:
    """
    Post a Block Kit message to Slack.
    `blocks` should be a dict with a "blocks" list (and optional "text").
    """
    if not SLACK_BOT_TOKEN:
        throw = "Missing SLACK_BOT_TOKEN"
        raise SlackError(throw)

    payload = {
        "channel": channel or SLACK_CHANNEL_ID,
        "blocks": blocks.get("blocks", []),
        "text": blocks.get("text", "TitleGate"),
    }

    async def _request():
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                content=json.dumps(payload).encode("utf-8"),
            )
            data = r.json()
            if not data.get("ok"):
                raise SlackError(f"chat.postMessage failed: {data}")
            return data

    return await _request()


def titlegate_blocks(article: Dict) -> Dict:
    """
    Build a Block Kit card with A/B/C approve buttons and Edit/Regenerate actions.
    Expects `article` to contain: article_id, title, short_description, reporter_name, canonical_url, proposed_titles.
    """
    a_id = article["article_id"]
    titles = (article.get("proposed_titles") or [])[:3]
    tA, tB, tC = (titles + ["", "", ""])[:3]
    summary = article.get("short_description", "")
    url = article.get("canonical_url", "")
    title = article.get("title", "")
    src = article.get("reporter_name", "")

    return {
        "text": "TitleGate",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Review Title Options*\n*Article:* <{url}|{title}>\n*Summary:* {summary}\n*Reporter:* {src}",
                },
            },
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*A*\n{tA}"}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve A"},
                        "style": "primary",
                        "value": json.dumps({"action": "approve", "choice": "A", "article_id": a_id}),
                        "action_id": "approve_a",
                    }
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*B*\n{tB}"}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve B"},
                        "style": "primary",
                        "value": json.dumps({"action": "approve", "choice": "B", "article_id": a_id}),
                        "action_id": "approve_b",
                    }
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*C*\n{tC}"}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve C"},
                        "style": "primary",
                        "value": json.dumps({"action": "approve", "choice": "C", "article_id": a_id}),
                        "action_id": "approve_c",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Edit Title"},
                        "value": json.dumps({"action": "edit", "article_id": a_id}),
                        "action_id": "edit_title",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Regenerate"},
                        "value": json.dumps({"action": "regen", "article_id": a_id}),
                        "action_id": "regen_title",
                    },
                ],
            },
        ],
    }


async def open_edit_modal(trigger_id: str, article_id: str, current: str) -> None:
    """
    Open a Slack modal with a single text input for the edited title.
    """
    view = {
        "type": "modal",
        "callback_id": "edit_submit",
        "private_metadata": json.dumps({"article_id": article_id}),
        "title": {"type": "plain_text", "text": "Edit Title"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "title_blk",
                "label": {"type": "plain_text", "text": "New Title (<= 60 chars)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "title_in",
                    "initial_value": (current or "")[:60],
                },
            }
        ],
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://slack.com/api/views.open",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            content=json.dumps({"trigger_id": trigger_id, "view": view}).encode("utf-8"),
        )
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"views.open failed: {data}")