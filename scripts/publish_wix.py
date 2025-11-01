# scripts/publish_wix.py
from __future__ import annotations
import os, asyncio, sys, json
import boto3
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeDeserializer
from tools import wix
from tools import slack as slk  # optional for "Published ✅" messages

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
ARTICLES_TABLE = os.getenv("ARTICLES_TABLE", "ai-gen-articles")
SLACK_POST = bool(os.getenv("PUBLISH_POST_TO_SLACK", "false").lower() in ("1","true","yes"))

deser = TypeDeserializer()

def _unmarshal(item):
    return {k: deser.deserialize(v) for k, v in item.items()}

async def publish_one(ddb, article: dict) -> dict:
    payload = wix.build_payload(article)
    prior_id = article.get("wix_id")
    res = await wix.create_or_update_item(payload, prior_id)
    # res should contain "id" for the Wix item
    wix_id = res.get("id") or res.get("_id")
    if not wix_id:
        raise wix.WixError(f"Missing id in Wix response: {res}")
    # update DDB row
    ddb.update_item(
        TableName=ARTICLES_TABLE,
        Key={"id" if "id" in article else "article_id": {"S": article.get("article_id") or article.get("id")}},
        UpdateExpression="SET #s = :s, wix_id = :wid",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": {"S": "published"},
            ":wid": {"S": wix_id},
        },
    )
    # optional Slack note
    if SLكثرack:=os.getenv("SLACK_BOT_TOKEN") and slk:
        msg = f"✅ *Published:* <{res.get('data',{}).get('url','') or article.get('canonical_url','')}>"
        try:
            asyncio.create_task(slk.post_message({"text": msg, "blocks": [{"type":"section","text":{"type":"mrkdwn","text":msg}}]}))
        except Exception:
            pass
    return {"article_id": article.get("article_id"), "wix_id": wix_id}

async def main_async():
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    # fetch approved items
    resp = ddb.scan(
        TableName=ARTICLES_TABLE,
        FilterExpression=Attr("status").eq("approved"),
    )
    items = [_unmarshal(it) for it in resp.get("Items", [])]
    if not items:
        print("No approved items to publish.")
        return 0

    print(f"Found {len(items)} approved item(s) to publish.")
    results = []
    for art in items:
        try:
            res = await publish_one(ddb, art)
            results.append(res)
            print(f"Published {res['article_id']} → wix_id={res['wix_id']}")
        except Exception as e:
            print(f"[ERROR] {art.get('article_id')}: {e}", file=sys.stderr)

    print("Done.")
    return 0

def main():
    return asyncio.run(main_async())

if __name__ == "__main__":
    raise SystemExit(main())
