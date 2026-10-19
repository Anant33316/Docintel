"""Load a document's extraction result (S3 ``results/{docId}.json``), with
backward compatibility for documents processed before the upgrade."""
import json

from . import aws
from .chunking import page_lines_from_text


class NotReady(Exception):
    pass


def load(item):
    if item.get("status") != "COMPLETED" or not item.get("resultKey"):
        raise NotReady(f"document is {item.get('status')}")
    body = aws.client("s3").get_object(Bucket=aws.bucket(), Key=item["resultKey"])["Body"].read()
    data = json.loads(body)
    data.setdefault("keyValues", {})
    data.setdefault("tables", [])
    if not data.get("pageLines"):
        data["pageLines"] = page_lines_from_text(data.get("text", ""))
    if not data.get("fields"):
        data["fields"] = [{"fieldId": f"legacy{i:03d}", "key": k, "value": v, "confidence": None, "page": None}
                          for i, (k, v) in enumerate(data["keyValues"].items())]
    return data
