"""DynamoDB helpers: float<->Decimal conversion, pagination, time helpers."""
import time
from datetime import datetime, timezone
from decimal import Decimal


def to_ddb(obj, places=4):
    """Recursively convert floats to Decimal (boto3 rejects floats)."""
    if isinstance(obj, float):
        return Decimal(str(round(obj, places)))
    if isinstance(obj, dict):
        return {k: to_ddb(v, places) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_ddb(v, places) for v in obj]
    return obj


def from_ddb(obj):
    """Recursively convert Decimal -> int/float and boto3 Binary -> bytes."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [from_ddb(v) for v in obj]
    if isinstance(obj, set):
        return sorted(from_ddb(v) for v in obj)
    if hasattr(obj, "value") and isinstance(getattr(obj, "value"), (bytes, bytearray)):
        return bytes(obj.value)
    return obj


def json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (bytes, bytearray)):
        return None
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def query_all(table, max_items=1000, **kwargs):
    items, start = [], None
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start or len(items) >= max_items:
            return items[:max_items]


def scan_all(table, max_items=5000, **kwargs):
    items, start = [], None
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start or len(items) >= max_items:
            return items[:max_items], bool(start)


def now_s():
    return int(time.time())


def now_ms():
    return int(time.time() * 1000)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ttl_after_days(days):
    return now_s() + int(days) * 86400
