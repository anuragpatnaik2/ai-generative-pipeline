import asyncio, json, os
from tools import storage
from tools.slack import titlegate_blocks, post_message

async def main():
    import boto3
    ddb = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION","us-east-1"))
    # GSI is optional; simple scan for now (small volume)
    table = os.getenv("ARTICLES_TABLE","ai-gen-articles")
    resp = ddb.scan(TableName=table, FilterExpression="attribute_exists(#s) AND #s = :w",
                    ExpressionAttributeNames={"#s":"status"},
                    ExpressionAttributeValues={":w":{"S":"awaiting_approval"}})
    items = resp.get("Items",[])
    if not items:
        print("No awaiting_approval items.")
        return
    # Minimal DDB JSON → python
    def unmarshal(it):
        out={}
        for k,v in it.items():
            (t,val), = list(v.items())
            out[k] = val
        return out
    awaitables=[]
    for it in map(unmarshal, items):
        blocks = titlegate_blocks(it)
        awaitables.append(post_message(blocks))
    # send in sequence to keep Slack happy
    for coro in awaitables:
        await coro
        print("Posted TitleGate card.")
asyncio.run(main())
