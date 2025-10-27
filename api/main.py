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

import re
from urllib.parse import parse_qs  # manual form parsing, no python-multipart needed

def _clean_title(text: str) -> str:
    """Trim, strip quotes/whitespace, and clamp to 60 chars."""
    s = "" if text is None else str(text)
    s = s.strip()
    # (optional) collapse whitespace
    s = re.sub(r"\s+", " ", s)
    # strip surrounding quotes if present
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s[:60]

@app.get("/")
def root():
    return {"ok": True}

@app.post("/resume")  # Slack interactivity hits here
async def resume(request: Request):
    """
    Handles Slack Interactivity:
      • Empty body (initial URL verification) → 200 OK
      • block_actions (Approve A/B/C, Edit) → verify, parse, update DDB, respond
      • view_submission (Edit modal submit) → update DDB, ack
    Uses manual form parsing (no python-multipart required).
    """
    import boto3

    raw = await request.body()
    print("[resume] hit; bytes =", len(raw))

    # 0) Slack’s URL verification: empty body ⇒ respond 200 quickly
    if not raw:
        print("[resume] empty body → ack")
        return {"ok": True}

    # 1) Verify Slack signature unless disabled
    if os.getenv("SLACK_VERIFY", "on").lower() == "on":
        try:
            slk.verify_signature(request.headers, raw)
        except Exception as e:
            print("[resume] verify FAILED:", e)
            raise HTTPException(status_code=401, detail=f"Slack verify failed: {e}")
    else:
        print("[resume] SLACK_VERIFY=off (skipping signature)")

    # 2) Parse payload (Slack posts application/x-www-form-urlencoded with a single 'payload' field)
    try:
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            payload = await request.json()
        else:
            form = parse_qs(raw.decode("utf-8", errors="ignore"))
            payload_str = (form.get("payload") or [None])[0]
            if not payload_str:
                print("[resume] no 'payload' field; form keys:", list(form.keys()))
                return {"ok": True}
            payload = json.loads(payload_str)
    except Exception as e:
        print("[resume] payload parse error:", e)
        return {"ok": True}

    print("[resume] type =", payload.get("type"))

    # 3) Common DDB helpers
    region = os.getenv("AWS_REGION", "us-east-1")
    table = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
    ddb = boto3.client("dynamodb", region_name=region)

    def _S(item, key, default=""):
        v = item.get(key)
        return v.get("S") if isinstance(v, dict) and isinstance(v.get("S"), str) else default

    def _L(item, key):
        arr = item.get(key, {}).get("L")
        if not arr:
            return []
        return [(x.get("S") if isinstance(x, dict) else str(x)) for x in arr]

    # 4) Handle block actions (Approve / Edit buttons)
    if payload.get("type") == "block_actions":
        acts = payload.get("actions") or []
        print("[resume] actions.count =", len(acts))
        act = acts[0] if acts else {}

        data = {}
        try:
            val = act.get("value")
            if isinstance(val, str):
                data = json.loads(val)
        except Exception as e:
            print("[resume] value JSON parse fail:", e)

        # Fallback to action_id suffix (_a/_b/_c)
        aid = (act.get("action_id") or "").lower()
        if "choice" not in data:
            if aid.endswith("_a"):
                data["choice"] = "A"
            elif aid endswith("_b"):
                data["choice"] = "B"  # ensure this has the dot in .endswith
            elif aid.endswith("_c"):
                data["choice"] = "C"
        print("[resume] data =", data)

        # 4a) Edit → open modal
        if data.get("action") == "edit":
            art_id = data.get("article_id") or ""
            current_title = ""
            try:
                rec = ddb.get_item(
                    TableName=table,
                    Key={"article_id": {"S": art_id}},
                ).get("Item") or {}
                current_title = _S(rec, "title", "")
            except Exception as e:
                print("[resume] DDB get_item error:", e)
            try:
                await slk.open_button(just_trigger_id:=payload.get("trigger_id"), article_id=art_id, current=current_title)  # keep your original slk.open_edit_modal if different
            except Exception as e:
                print("[resume] views.open error:", e)
            return {"response_action": "clear"}

        # 4b) Approve → update DDB
        if data.get("action") in ("approve", "eqWjRollover"):  # keep alt id if you used one in the button value
            art_id = data.get("article_id") or data.get("id")
            choice = (data.get("category") or data.get("choice") or "A").strip().upper()
            print(f"[resume] APPROVE art_id={art_id} choice={self_choice:=choice}")
            if not art_id:
                print("[resume] missing article_id")
                return {"text": "Unable to identify article."}

            try:
                item = ddb.get_item(
                    TableName=table,
                    Key={"article_id": {"S": art_id}},
                ).get("Item")
                if not item:
                    print(f"[resume] not found: {art_id}")
                    return {"text": "Article not found."}

                titles = _L(item, "proposed_titles")
                idx = {"A": 0, "B": 1, "C": 2}.get(choice, 0)
                raw_title = titles[idx] if idx < len(titles) else _S(item, "title")
                new_title = _clean_title(raw_title)
                if not new_title:
                    new_title = "Approved title"

                print(f"[resume] updating {art_id} → '{new_title}' (table {table})")
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

                # Replace original Slack message so you see success inline
                return {"response_action": "update", "text": f"✅ Approved: {new_title}"}

            except Exception as e:
                print("[resume] DDB update error:", e)
                return {"text": f"Update failed: {e}"}

        print("[resume] unhandled action → ack")
        return {"text": "OK"}

    # 5) Handle modal submission (Edit title)
    if payload.get("type") == "view_submission" and payload.get("view", {}).get("callback_id") == "edit_submit":
        try:
            meta = json.loads(payload["view"].get("private_metadata", "{}"))
            art_id = meta.get("article_id", "")
            new_title = _clean_title(
                (payload["view"]["state"]["values"]["title_blk"]["title_in"]["value"] or "")
            )
            print(f"[resume] EDIT_SUBMIT {art_id} → '{new_title}'")
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

    print("[resume] non-interactive payload → ack")
    return {"ok": True}
