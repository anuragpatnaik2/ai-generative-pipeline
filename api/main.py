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
    import boto3  # ensure available when approve path runs

    raw = await request.body()
    print("[resume] hit; raw_len=", len(raw))

    # Slack initial URL save ping (empty body) — always ack
    if not raw:
        print("[resume] empty body -> ack for Slack URL save")
        return {"ok": True}

    # Signature verify (set SLACK_VERIFY=off to bypass during testing)
    if os.getenv("SLACK_VERIFY", "on").lower() == "on":
        if slk is None:
            print("[resume] ERROR: slack verifier not available")
            raise HTTPException(status_code=500, detail="Slack verifier not available")
        try:
            slk.verify_signature(request.headers, raw)
        except Exception as e:
            print("[resume] signature verify FAILED:", e)
            raise HTTPException(status_code=401, detail=f"Slack verify failed: {e}")
    else:
        print("[resume] WARNING: SLACK_VERIFY=off (skipping signature check)")

    # Parse Slack payload
    try:
        form = await request.form()
        payload_txt = form.get("payload", "{}")
        print("[resume] payload snippet:", (payload_txt[:200] + ("…"
              if len(payload_txt) > 200 else "")))
        payload = json.loads(payload_txt)
    except Exception as e:
        print("[resume] payload parse error:", e)
        return {"ok": True}

    ptype = payload.get("type")
    print("[resume] payload.type =", ptype)

    # ===== Block actions (buttons) =====
    if ptype == "block_actions":
        actions = payload.get("actions") or []
        print("[resume] actions.count =", len(actions))
        action = actions[0] if actions else {}
        if action:
            # Do not log full value (may be long); show short preview
            val = action.get("value")
            print("[resume] first action.id =", action.get("action_id"),
                  " value_snippet=", (val[:120] + "…") if isinstance(val, str) and len(val) > 120 else val)

        # Parse embedded JSON value (our button value)
        try:
            data = json.loads(action.get("value", "{}")) if isinstance(action.get("value"), str) else {}
        except Exception as e:
            print("[resume] json.loads(action.value) failed:", e)
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

        print("[resume] parsed data =", data)

        # Handle edit → open modal
        if data.get("action") == "edit":
            if slk is None or storage is None:
                print("[resume] EDIT: missing slk/storage, skipping modal open")
                return {"response_action": "clear"}
            trigger_id = payload.get("trigger_id")
            art_id = data.get("article_id", "")
            current = (storage.getArticle(art_id) if storage else {}) or {}
            cur_title = current.get("title", "")
            print(f"[resume] EDIT: article_id={art_id} current_title='{cur_title[:80]}'")
            try:
                await slk.open_edit_modal(trigger_id, art_id, cur_title)
            except Exception as e:
                print("[resume] EDIT: open_edit_modal error:", e)
            return {"response_action": "clear"}

        # Handle approve → update DynamoDB directly
        if data.get("action") == "approve":
            art_id = data.get("article_id")
            choice = (data.get("choice") or "A").upper()
            print(f"[resume] APPROVE: article_id={art_id} choice={choice}")
            if not art_id:
                print("[resume] APPROVE: missing article_id in action data")
                return {"text": "Unable to identify article_id from action."}

            region = os.getenv("AWS_REGION", "us-east-1")
            table  = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
            ddb    = boto3.client("dynamodb", region_name=region)
            print(f"[resume] APPROVE: DDB table={table} region={region}")

            try:
                got = ddb.get_item(
                    TableName=table, Key={"article_id": {"S": art_id}}
                ).get("Item")
                if not got:
                    print(f"[resume] APPROVE: DDB get_item returned empty for id={art_id}")
                    return {"text": "Article not found."}

                # tiny unmarshallers
                def _S(item, key, default=""):
                    v = item.get(key)
                    return v.get("S") if isinstance(v, dict) and isinstance(v.get("S"), str) else default
                def _L(item, key):
                    v = item.get("L") if isinstance(item.get(key), dict) else None
                    if v is None:
                        v = item.get(key, {}).get("L")
                    if not v:
                        return []
                    return [ (x.get("S") if isinstance(x, dict) else str(x)) for x in v ]

                proposed       = (got.get("proposed_titles", {}).get("L") and
                                   [x.get("S") for x in got["proposed_titles"]["L"]] ) or []
                current_title  = _S(got, "title", "")
                idx_map        = {"A": 0, "B": 1, "C": 2}
                idx            = idx_map.get(choice, 0)
                approved_title = (proposed[idx] if len(proposed) > idx and proposed[idx] else current_title)[:60]
                if not approved_title:
                    approved_title = "Approved title"

                print(f"[resume] APPROVE: updating id={art_id} -> '{approved_title}'")
                ddb.update_item(
                    TableName=table,
                    Key={"article_id": {"S": art_id}},
                    UpdateExpression="SET #s = :s, approved_title = :t, title = :t",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":s": {"S": "approved"},
                        ":t": {"S": approved_title},
                    },
                )

                # Ask Slack to replace the original message so you see it update immediately.
                return {
                    "response_action": "update",
                    "text": f"✅ Approved: {approved_title}"
                }

            except Exception as e:
                print("[resume] APPROVE: DDB update error:", e)
                return {"text": f"Update failed: {e}"}

        # Handle regenerate marker
        if data.get("action") == "regen":
            art_id = data.get("article_id")
            print(f"[resume] REGEN requested for {art_id}")
            if storage is not None and art_id:
                try:
                    storage.update_article(art_id, status="awaiting_approval", needs_regen=True)
                except Exception as e:
                    print("[resume] REGEN: storage.update_article error:", e)
            return {"text": "🔁 Will regenerate titles soon."}

        print("[resume] Unhandled block action payload:", data)
        return {"text": "Unhandled action"}

    # ===== Modal submission (view_submission)
    if ptype == "view_submission" and payload.get("view", {}).get("callback_id") == "edit_submit":
        try:
            pm        = json.loads(payload["view"].get("private_metadata", "{}"))
            art_id    = pm.get("article_id", "")
            new_title = payload["view"]["state"]["values"]["title_blk"]["title_in"]["value"].strip()[:60]
            print(f"[resume] EDIT_SUBMIT: id={art_id} -> '{new_title}'")

            region = os.getenv("AWS_REGION", "us-east-1")
            table  = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
            ddb    = boto3.client("dynamodb", region_name=region)

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

    print("[resume] Non-action payload; ack")
    return {"ok": True}
