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

# --- BEGIN /resume (drop-in replacement) ---
from fastapi import HTTPException, Request
import os, json, boto3

@app.post("/resume")  # Slack interactivity hits here
async def resume(request: Request):
    raw = await request.body()

    # Slack's initial "save URL" ping can be an empty body — allow it.
    if not raw:
        return {"ok": True}

    # Verify Slack signature unless you've explicitly disabled it via SLACK_VERIFY=off
    if os.getenv("SLACK_VERIFY", "on").lower() == "on":
        try:
            slk.verify_signature(request.headers, raw)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Slack verify failed: {e}")

    # Parse payload
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
    except Exception:
        payload = {}

    ptype = payload.get("type")

    # ====== BUTTON CLICKS ======
    if ptype == "block_actions":
        actions = payload.get("actions") or []
        action  = actions[0] if actions else {}

        # Try to parse the embedded JSON value first (what we set in the button)
        try:
            data = json.loads(action.get("value", "{}"))
        except Exception:
            data = {}

        # Fallback: derive the choice from action_id if value is missing
        action_id = (action.get("action_id") or "").lower()
        if "choice" not in data:
            if action_id.endswith("_a"):
                data["choice"] = "A"
            elif action_id.endswith("_b"):
                data["choice"] = "B"
            elif action_id.endswith("_c"):
                data["choice"] = "C"

        # EDIT: open modal
        if data.get("action") == "edit":
            trigger_id = payload.get("trigger_id")
            art_id     = data.get("article_id", "")
            a = storage.get_article(art_id) if storage else {}
            current = (a or {}).get("title", "")
            await slk.open_edit_modal(trigger_id, art_id, current)
            return {"response_action": "clear"}

        # APPROVE: flip status + approved_title directly in DynamoDB
        if data.get("action") == "approve":
            art_id = data.get("article_id")
            choice = data.get("choice") or "A"

            if not art_id:
                return {"text": "Unable to identify article_id from action."}

            # Fetch item
            ddb   = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
            table = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
            got   = ddb.get_item(TableName=table, Key={"article_id": {"S": art_id}}).get("Item")

            if not got:
                return {"text": "Article not found."}

            # Unmarshall a few fields
            def _S(item, key, default=""):
                v = item.get(key)
                return v.get("S") if isinstance(v, dict) and "S" in v else default

            def _L(item, key):
                v = item.get(key)
                if isinstance(v, dict) and "L" in v:
                    return [x.get("S") for x in v["L"] if isinstance(x, dict) and "S" in x]
                return []

            proposed      = _L(got, "proposed_titles")
            current_title = _S(got, "title", "")
            idx_map       = {"A": 0, "B": 1, "C": 2}
            idx           = idx_map.get(choice, 0)

            if proposed and 0 <= idx < len(proposed) and proposed[idx]:
                approved = proposed[idx][:60]
            else:
                approved = (current_title or "Approved Title")[:60]

            # Persist update: status -> approved, set titles
            ddb.update_item(
                TableName=table,
                Key={"article_id": {"S": art_id}},
                UpdateExpression="SET #s = :s, approved_title = :t, #t = :t",
                ExpressionAttributeNames={"#s": "status", "#t": "title"},
                ExpressionAttributeValues={
                    ":s": {"S": "approved"},
                    ":t": {"S": approved},
                },
            )
            return {"text": f"✅ Approved: {approved}"}

        # REGENERATE requested (just mark a flag; your follow-up job can refresh proposed_titles)
        if data.get("action") == "regen":
            art_id = data.get("article_id")
            if art_id and storage:
                storage.update_article(art_id, status="awaiting_approval", needs_regen=True)
            return {"text": "🔁 Will regenerate titles soon."}

        # Unhandled action
        return {"text": "Unhandled action"}

    # ====== MODAL SUBMIT (Edit Title) ======
    if ptype == "view_submission" and payload.get("view", {}).get("callback_id") == "edit_submit":
        try:
            pm = json.loads(payload["view"].get("private_metadata", "{}"))
            art_id = pm.get("article_id", "")
            new_title = payload["view"]["state"]["values"]["title_blk"]["title_in"]["value"].strip()[:60]

            # Update via DynamoDB directly (works whether or not storage helper exists)
            ddb   = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
            table = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
            ddb.update_item(
                TableName=table,
                Key={"article_id": {"S": art_id}},
                UpdateExpression="SET #s = :s, approved_title = :t, #t = :t",
                ExpressionAttributeNames={"#s": "status", "#t": "title"},
                ExpressionAttributeValues={":s": {"S": "approved"}, ":t": {"S": new_title}},
            )
        except Exception:
            # Always ack so Slack isn't unhappy; log in real app.
            return {"response_action": "clear"}
        return {"response_action": "clear"}

    # Default ack
    return {"ok": True}
# --- END /resume ---


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
