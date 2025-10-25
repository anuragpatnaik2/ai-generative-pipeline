# api/main.py  — stable & Py3.8-compatible
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request
import os, json, boto3

# Try to import helpers; fall back to None so import never crashes
try:
    from tools import storage
except Exception:
    storage = None
try:
    from tools import slack as slk
except Exception:
    slk = None

app = FastAPI()

def _check_auth(auth: Optional[str], token_env: str = "APP_AUTH_TOKEN") -> None:
    tok = os.getenv(token_env)
    if not tok:
        return
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Missing/invalid Authorization header")
    if auth.split(" ", 1)[1] != tok:
        raise HTTPException(status_code=403, detail="Invalid token")

@app.get("/")
def root():
    return {"ok": True}

@app.post("/run/daily")
async def run_daily(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"status": "ok"}

@app.post("/resume")
async def resume(request: Request):
    raw = await request.body()
    # Slack initial URL save ping (empty body) — always ack
    if not raw:
        return {"ok": True}

    # Signature verify (set SLACK_VERIFY=off to bypass during testing)
    if os.getenv("SLACK_VERIFY", "on").lower() == "on":
        if slk is None:
            raise HTTPException(status_code=500, detail="Slack verifier not available")
        try:
            slk.verify_signature(request.headers, raw)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Slack verify failed: {e}")

    # Parse Slack payload
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
    except Exception:
        payload = {}

    ptype = payload.get("type")

    # ===== Block actions (buttons) =====
    if ptype == "block_actions":
        actions = payload.get("actions") or []
        action  = actions[0] if actions else {}

        # Parse embedded JSON value (our button value)
        try:
            data = json.loads(action.get("value", "{}"))
        except Exception:
            data = {}

        # Fallback: infer choice from action_id
        action_id = (action.get("action_id") or "").lower()
        if "choice" not in data:
            if action_id.endswith("_a"):
                data["choice"] = "A"
            elif action_id.endswith("_b"):
                data["choice"] = "B"
            elif action_id.endswith("_c"):
                data["choice"] = "C"

        # Handle edit → open modal
        if data.get("action") == "edit":
            if slk is None or storage is None:
                return {"response_action": "clear"}
            trigger_id = payload.get("trigger_id")
            art_id = data.get("article_id", "")
            a = storage.get_article(art_id) if storage else {}
            current = (a or {}).get("title", "")
            await slk.open_edit_modal(trigger_id, art_id, current)
            return {"response_action": "clear"}

        # Handle approve → update DynamoDB directly
        if data.get("action") == "approve":
            art_id = data.get("article_id")
            choice = data.get("choice") or "A"
            if not art_id:
                return {"text": "Unable to identify article_id from action."}

            region = os.getenv("AWS_REGION", "us-east-1")
            table  = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
            ddb    = boto3.client("dynamodb", region_name=region)

            got = ddb.get_item(
                TableName=table, Key={"article_id": {"S": art_id}}
            ).get("Item")
            if not got:
                return {"text": "Article not found."}

            # tiny unmarshallers
            def _S(item, key, default=""):
                v = item.get(key)
                return v.get("S") if isinstance(v, dict) and "S" in v else default
            def _L(item, key):
                v = item.get(key)
                if isinstance(v, dict) and "L" in v:
                    return [x.get("S") for x in v["L"] if isinstance(x, dict) and "S" in x]
                return []

            proposed       = _L(got, "proposed_titles")
            current_title  = _S(got, "title", "")
            idx_map        = {"A": 0, "B": 1, "C": 2}
            idx            = idx_map.get(choice, 0)
            approved_title = ""
            if proposed and 0 <= idx < len(proposed) and proposed[idx]:
                approved_title = proposed[idx][:60]
            else:
                approved_title = (current_title or "Approved Title")[:60]

            ddb.update_item(
                TableName=table,
                Key={"article_id": {"S": art_id}},
                UpdateExpression="SET #s = :s, approved_title = :t, #t = :t",
                ExpressionAttributeNames={"#s": "status", "#t": "title"},
                ExpressionAttributeValues={
                    ":s": {"S": "approved"},
                    ":t": {"S": approved_title},
                },
            )
            return {"text": f"✅ Approved: {approved_title}"}

        # Handle regenerate marker
        if data.get("action") == "regen":
            art_id = data.get("article_id")
            if storage is not None and art_id:
                storage.update_article(art_id, status="awaiting_approval", needs_regen=True)
            return {"text": "🔁 Will regenerate titles soon."}

        return {"text": "Unhandled action"}

    # ===== Modal submission (Edit) =====
    if ptype == "view_submission" and payload.get("view", {}).get("callback_id") == "edit_submit":
        try:
            pm        = json.loads(payload["view"].get("private_metadata", "{}"))
            art_id    = pm.get("article_id", "")
            new_title = payload["view"]["state"]["values"]["title_blk"]["title_in"]["value"].strip()[:60]

            region = os.getenv("AWS_REGION", "us-east-1")
            table  = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
            ddb    = boto3.client("dynamodb", region_name=region)
            ddb.update_item(
                TableName=table,
                Key={"article_id": {"S": art_id}},
                UpdateExpression="SET #s = :s, approved_title = :t, #t = :t",
                ExpressionAttributeNames={"#s": "status", "#t": "title"},
                ExpressionAttributeValues={
                    ":s": {"S": "approved"},
                    ":t": {"S": new_title},
                },
            )
        except Exception:
            return {"response_action": "clear"}
        return {"response_action": "clear"}

    return {"ok": True}