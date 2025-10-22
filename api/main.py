# api/main.py
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request
import json, os
from tools import storage
from tools import slack as slk

app = FastAPI()

def _check_auth(auth: Optional[str], token_env: str = "APP_AUTH_TOKEN"):
    tok = os.getenv(token_env)
    if not tok: return
    if not auth or not auth.startswith("Bearer ") or auth.split(" ",1)[1] != tok:
        raise HTTPException(status_code=403, detail="Bad token")

@app.post("/run/daily")
async def run_daily(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"status":"ok"}  # unchanged for now

@app.post("/resume")  # Slack interactivity hits here
async def resume(request: Request):
    raw = await request.body()
    try:
        slk.verify_signature(request.headers, raw)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Slack verify failed: {e}")

    form = await request.form()
    payload = json.loads(form.get("payload","{}"))

    # 1) Button clicks (block_actions)
    if payload.get("type") == "block_actions":
        action = payload["actions"][0]
        data = json.loads(action.get("value","{}"))
        user = payload.get("user",{}).get("username") or payload.get("user",{}).get("name","")
        # Edit → open modal
        if data.get("action") == "edit":
            trigger_id = payload.get("trigger_id")
            a = storage.get_article(data["article_id"]) or {}
            await slk.open_edit_modal(trigger_id, data["article_id"], a.get("title",""))
            return {"response_action":"clear"}  # acknowledge

        # Approve A/B/C
        if data.get("action") == "approve":
            a = storage.get_article(data["article_id"])
            if not a: return {"text":"Article not found."}
            titles = (a.get("proposed_titles") or []) + ["","",""]
            idx = {"A":0,"B":1,"C":2}.get(data.get("choice","A"),0)
            approved = titles[idx][:60]
            storage.update_article(a["article_id"],
                approved_title=approved, title=approved, status="approved")
            return {"text": f"✅ Approved: {approved}"}

        # Regenerate title set (call your model again)
        if data.get("action") == "regen":
            # Minimal: mark needs_regen; a follow-up job can rewrite proposed_titles
            storage.update_article(data["article_id"], status="awaiting_approval", needs_regen=True)
            return {"text": "🔁 Will regenerate titles soon."}

        return {"text":"Unhandled action"}

    # 2) Modal submission (view_submission)
    if payload.get("type") == "view_submission" and payload.get("view",{}).get("callback_id") == "edit_submit":
        pm = json.loads(payload["view"].get("private_metadata","{}"))
        a_id = pm.get("article_id")
        new_title = payload["view"]["state"]["values"]["title_blk"]["title_in"]["value"].strip()[:60]
        storage.update_article(a_id, approved_title=new_title, title=new_title, status="approved")
        return {"response_action":"clear"}

    # 3) URL verification (rare) or other
    return {"ok": True}from fastapi import FastAPI, Header, HTTPException

app = FastAPI()

def _check_auth(auth: str | None, token_env: str = "APP_AUTH_TOKEN"):
    import os
    if not os.getenv(token_env):
        return  # dev: skip if not set
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth")
    if auth.split(" ", 1)[1] != os.getenv(token_env):
        raise HTTPException(status_code=403, detail="Bad token")

@app.post("/run/daily")
def run_daily(authorization: str | None = Header(None)):
    _check_auth(authorization)
    return {"status": "stub - daily run accepted"}

@app.post("/resume")
def resume():
    # Day 4/5: Slack interactivity lands here (we'll verify signature later)
    return {"status": "stub - resume accepted"}

@app.post("/run/weekly")
def run_weekly(authorization: str | None = Header(None)):
    _check_auth(authorization)
    return {"status": "stub - weekly run accepted"}
