from fastapi import FastAPI, Header, HTTPException

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
