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

@app.post("/resume")
async def resume(request: Request):
    import boto3
    from urllib.parse import parse_qs

    raw = await request.body()
    print("[resume] hit; len=", len(raw))

    # Slack initial URL check: empty body → must return 200 fast
    if not raw:
        print("[resume] empty body -> ack")
        return {"ok": True}

    # Verify Slack signature (unless disabled explicitly)
    if os.getenv("SLACK_VERIFY", "on").lower() == "on":
        try:
            slk.verify_signature(request.headers, raw)
        } except Exception as e:
            print("[resume] verify FAILED:", e)
            raise HTTPException(status_code=401, detail=f"Slack verify failed: {e}")
    else:
        print("[resume] SLACK_VERIFY=off (skipping signature)")

    # --- Parse payload without python-multipart ---
    payload: dict = {}
    try:
        ct = request.headers.get("content-type", "")
        if "application/json" in ct.lower():
            payload = await request.json()
        else:
            form = parse_qs(raw.decode("utf-8", errors="ignore"))
            p = form.get("payload", [None])[0]
            if not p:
                print("[resume] no 'payload' field in form; keys:", list(form.keys()))
                return {"ok": True}
            payload = json.loads(p)
    except Exception as e:
        print("[resume] payload parse error:", e)
        return {"ok": True}

    print("[resume] type =", payload.get("type"))

    # ---- Common DDB helpers ----
    region = os.getenv("AWS_REGION", "us-east-1")
    table  = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
    ddb    = boto3.client("dynamodb", region_name=region)

    def _S(item, key, default=""):
        v = item.get(key)
        return v.get("S") if isinstance(v, dict) and isinstance(v.get("S"), str) else default

    def _L(item, key):
        L = item.get(key, {}).get("L")
        if not L: return []
        return [ (x.get("S") if isinstance(x, dict) else str(x)) for x in L ]

    # ---- Handle block actions (buttons) ----
    if payload.get("type") == "block_actions":
        acts = payload.get("actions") or []
        print("[resume] actions.count =", len(acts))
        act = acts[0] if acts else {}
        # Parse the tiny JSON we stuffed into the button's `value`
        data = {}
        try:
            if isinstance(act.get("value"), str):
                data = json.loads(act["value"])
        except Exception as e:
            print("[resume] act.value parse fail:", e)
        # Fallback to action_id suffix -> A/B/C
        aid = (act.get("action_id") or "").lower()
        if "choice" not in data:
            if aid.endswith("_a"): data["choice"] = "A"
            elif aid.endswith("_b"): data["choice"] = "B"
            elif aid.endswith("_c"): data["choice"] = "C"
        print("[resume] data =", data)

        # EDIT → open modal
        if data.get("action") == "edit":
            art_id = data.get("article_id") or ""
            cur = {}
            try:
                got = ddb.get_item(TableName=table, Key={"article_id": {"S": art_id}}).get("Item", {})
                cur = {"title": _S(got, "title", "")}
            except Exception as e:
                print("[resume] DDB get_item error:", e)
            trig = payload.get("trigger_id")
            try:
                await slk.open_edit_modal(trig, art_id, cur.get("title", ""))
            except Exception as e:
                print("[resume] views.open error:", e)
            return {"response_action": "clear"}

        # APPROVE → update DDB
        if data.get("action") == "approve":
            art_id = data.get("id") or data.get("article_id")
            choice = (data.get("choice") or "A").upper()
            print(f"[resume] APPROVE id={art_id} choice={choice}")
            if not art_id:
                print("[resume] missing article_id in data")
                return {"text": "Unable to identify article."}

            try:
                item = ddb.get_item(TableName=table, Key={"article_id": {"S": art_id}}).get("Item")
                if not item:
                    print(f"[resume] not found: {art_id}")
                    return {"text": "Article not found."}

                titles = _L(item, "proposed_titles")
                idx    = {"A":0,"B":1,"C":2}.get(choice, 0)
                new_title = (titles[idx] if idx < len(titles) and titles[idx] else _S(item, "title")).strip()[:60]
                if not new_title:
                    new_title = "Approved title"

                print(f"[resume] updating {art_id} -> '{new_title}' on table {table}")
                d = {
                    "TableName": table,
                    "Key": {"article_id": {"S": art_id}},
                    "UpdateExpression": "SET #s = :s, approved_title = :t, title = :t",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":s": {"S": "approved"},
                        ":t": {"S": new_title},
                    },
                }
                ddb.update_item(**d)

                # ask Slack to replace the original message so you see success inline
                return {"response_action": "update", "text": f"✅ Approved: {new_title}"}

            except Exception as e:
                print("[resume] DDB update error:", e)
                return {"text": f"Update failed: {e}"}

        # other action
        print("[resume] unhandled action -> ack")
        return {"text": "OK"}

    # ---- Handle modal submission ----
    if payload.get("type") == "view_submission" and payload.get("view", {}).get("callback_id") == "edit_submit":
        try:
            meta = json.loads(payload["view"].get("private_metadata", "{}"))
            art_id = meta.get("article_id", "")
            new_title = (payload["view"]["state"]["values"]["title_blk"]["title_in"]["value"] or "").strip()[:60]
            print(f"[resume] EDIT_SUBMIT id={art_id} -> '{new_title}'")
            if art_id and new_title:
                ddb.update_item(
                    TableName=table,
                    Key={"article_id": {"S": art_id}},
                    UpdateExpression="SET #s = :s, approved_title = :t, title = :t",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":s": {"S": "approved"},
                        ":t": {"S": new_title},
                    },
                )
        except Exception as e:
            print("[resume] EDIT_SUBMIT error:", e)
        return {"response_action": "clear"}

    print("[resume] non-interactive payload -> ack")
    return {"ok": True}