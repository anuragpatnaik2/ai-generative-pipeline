# api/main.py  (Py 3.8 compatible)
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request
import os, json

# Optional: only import these if the modules exist in your repo
try:
    from tools import storage
    from tools import slack as slk
except Exception:
    storage = None
    slk = None

app = FastAPI()


def _check_auth(auth: Optional[str], token_env: str = "APP_AUTH_TOKEN") -> None:
    """
    Optional bearer-token check for /run/* endpoints.
    If APP_AUTH_TOKEN is unset, the check is skipped (handy for local dev).
    """
    tok = os.getenv(token_env)
    if not tok:
        return
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Missing/invalid Authorization header")
    if auth.split(" ", 1)[1] != tok:
        raise HTTPException(status_code=403, detail="Bad token")


@app.get("/")
def root():
    # Simple health endpoint so App Runner health checks pass
    return {"ok": True}


@app.post("/run/daily")
async def run_daily(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    # TODO: kick off your daily job here (for now just ack)
    return {"status": "ok"}


@app.post("/resume")  # Slack Interactivity Request URL

async def resume(request: Request):
    """
    Slack calls this on button clicks & modal submits.
    Slack also pings this URL when you first save it in the app config; that ping
    may have an empty body. We must return 200 quickly in that case.
    """
    raw = await request.body()
    if not raw:
        # Let Slack save the URL during initial verification.
        return {"ok": True}

   # TEMPORARY BYPASS FOR DEBUG:
    if os.getenv("SLACK_VERIFY", "on").lower() == "off":
        pass
    else:
        try:
            slk.verify_signature(request.headers, raw)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Slack verify failed: {e}")
	
	
    # Verify Slack signature if our helper is available
    if slk is None:
        # Fail-safe: allow during setup; tighten once slk.verify_signature is present.
        pass
    else:
        try:
            slk.verify_signature(request.headers, raw)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Slack verify failed: {e}")

    # Parse the interactive payload
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
    except Exception:
        payload = {}

    ptype = payload.get("type")

    # 1) Button clicks (block_actions)
    if ptype == "block_actions":
        actions = payload.get("actions") or []
        action = actions[0] if actions else {}
        try:
            data = json.loads(action.get("value", "{}"))
        except Exception:
            data = {}

        # Edit → open modal
        if data.get("action") == "edit":
            if slk is None or storage is None:
                return {"response_action": "clear"}
            trigger_id = payload.get("trigger_id")
            art_id = data.get("article_id", "")
            a = (storage.get_article(art_id) if storage else {}) or {}
            current = a.get("title", "")
            await slk.open_edit_modal(trigger_id, art_id, current)
            return {"response_action": "clear"}  # acknowledge

        # Approve A/B/C
        if data.get("action") == "approve":
            if storage is None:
                return {"text": "Storage not available"}
            art_id = data.get("article_id", "")
            a = storage.get_article(art_id)
            if not a:
                return {"text": "Article not found."}
            titles = (a.get("proposed_titles") or []) + ["", "", ""]
            idx = {"A": 0, "B": 1, "C": 2}.get(data.get("choice", "A"), 0)
            approved = titles[idx][:60]
            storage.update_article(
                art_id,
                approved_title=approved,
                title=approved,
                status="approved",
            )
            return {"text": f"✅ Approved: {approved}"}

        # Regenerate requested
        if data.get("action") == "regen":
            if storage is None:
                return {"text": "Regenerate requested (storage not available)."}
            art_id = data.get("article_id", "")
            storage.update_article(
                art_id,
                status="awaiting_approval",
                needs_regen=True
            )
            return {"text": "🔁 Will regenerate titles soon."}

        # Unhandled
        return {"text": "Unhandled action"}

    # 2) Modal submission (view_submission)
    if ptype == "view_submission" and payload.get("view", {}).get("callback_id") == "edit_submit":
        if storage is None:
            return {"response_action": "clear"}
        try:
            pm = json.loads(payload["view"].get("private_metadata", "{}"))
            new_title = payload["view"]["state"]["values"]["title_blk"]["title_in"]["value"].strip()[:60]
            art_id = pm.get("article_id", "")
            storage.update_article(art_id, approved_title=new_title, title=new_title, status="approved")
        except Exception:
            # Always ack so Slack isn't unhappy; log in real app.
            return {"response_action": "clear"}
        return {"response_action": "clear"}

    # 3) Other events (or parsing failed): ack
    return {"ok": True}

from fastapi import Query

@app.get("/_diag")
def diag():
    """Show whether critical env vars are present in the running container."""
    return {
        "region": os.getenv("AWS_REGION"),
        "articles_table": os.getenv("ARTICLES_TABLE"),
        "runs_table": os.getenv("RUNS_TABLE"),
        "slack_signing_set": bool(os.getenv("SLACK_SIGNING_SECRET")),
        "slack_token_set": bool(os.getenv("SLACK_BOT_TOKEN")),
    }

@app.get("/_ddb_status")
def ddb_status():
    """Count items by status from INSIDE the container (proves table/region/role)."""
    import boto3
    from collections import Counter
    ddb = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION","us-east-1"))
    t   = os.getenv("ARTICLES_TABLE","ai-gen-articles")
    resp = ddb.scan(TableName=t, ProjectionExpression="#s", ExpressionAttributeNames={"#s":"status"})
    counts = Counter((it.get("status", {"S": "(none)"}))["S"] for it in resp.get("Items", []))
    return {"table": t, "counts": dict(counts)}

@app.post("/_force_approve")
def force_approve(aid: str = Query(..., description="article_id"),
                  title: str = Query(..., description="approved title")):
    """
    Force one row to approved from inside the running service using boto3 directly.
    This proves table/region/role/write access independent of tools.storage.
    """
    import os, datetime
    import boto3
    ddb   = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
    table = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
    now   = datetime.datetime.utcnow().replace(microsecond=False).isoformat() + "Z"

    try:
        ddb.update_item(
            TableName=table,
            Key={"article_id": {"S": aid}},
            UpdateExpression="SET #s = :s, approved_title = :t, #t = :t, updated_at = :u",
            ExpressionAttributeNames={"#s": "status", "#t": "title"},
            ExpressionAttributeValues={
                ":s": {"S": "approved"},
                ":t": {"S": title},
                ":u": {"S": now},
            },
        )
        got = ddb.get_item(TableName=table, Key={"article_id": {"S": aid}}).get("Item", {})
        return {
            "ok": True,
            "after": {
                "article_id": got.get("article_id", {}).get("S"),
                "status":      got.get("status", {}).get("S"),
                "approved_title": got.get("approved_title", {}).get("S"),
                "title":          got.get("title", {}).get("S"),
            },
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
