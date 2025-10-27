import os, json, time, hashlib
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError

REGION = os.getenv("AWS_REGION", "us-east-1")
ARTICLES_TABLE = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
RUNS_TABLE = os.getenv("RUNS_TABLE", "ai-gen-runs")
S3_BUCKET = os.getenv("S3_BUCKET")

_ddb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)
_articles = _ddb.Table(ARTICLES_TABLE)
_runs = _ddb.Table(RUNS_TABLE)

import os, json, time, hashlib
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError

REGION = os.getenv("AWS_REGION", "us-east-1")
ARTICLES_TABLE = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
RUNS_TABLE = os.getenv("RUNS_TABLE", "ai-gen-runs")
S3_BUCKET = os.getenv("S3_BUCKET")

_ddb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)
_articles = _ddb.Table(ARTICLES_TABLE)
_runs = _ddb.Table(RUNS_TABLE)
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
def article_id_from_url(url: str) -> str:
    h = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:24]
    return f"art_{h}"

def put_run(run_id: str, status: str = "started"):
    _runs.put_item(Item={
        "run_id": run_id,
        "status": status,
        "started_at": now_iso(),
    })

def update_run(run_id: str, **updates):
    expr = "SET " + ", ".join(f"{k}=:{k}" for k in updates)
    _runs.update_item(
        Key={"run_id": run_id},
        UpdateExpression=expr,
        ExpressionAttributeValues={f":{k}": v for k, v in updates.items()},
    )

def get_article(article_id: str):
    resp = _articles.get_item(Key={"article_id": article_id})
    return resp.get("Item")

def put_article(item: dict):
    # minimal required fields can be enforced later
    item.setdefault("created_at", now_iso())
    item.setdefault("updated_at", item["created_at"])
    _articles.put_item(Item=item)

def update_article(article_id: str, **updates):
    updates["updated_at"] = now_iso()
    expr = "SET " + ", ".join(f"{k}=:{k}" for k in updates)
    _articles.update_item(
        Key={"article_id": article_id},
        UpdateExpression=expr,
        ExpressionAttributeValues={f":{k}": v for k, v in updates.items()},
    )

def put_artifact(run_id: str, path: str, data: dict):
    if not S3_BUCKET:
        return
    key = f"{run_id}/{path}".lstrip("/")
    _s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{S3_BUCKET}/{key}"
