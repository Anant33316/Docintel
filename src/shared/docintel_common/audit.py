"""Audit trail: one DynamoDB record + one structured CloudWatch log line per event.

Content safety: ``details`` is sanitised so document text, questions, answers and
field values are never written to the audit trail or to logs; only identifiers,
counts, types and short enumerations survive.

Audit writes are best-effort: a failure is logged at ERROR level (visible in
CloudWatch Logs; see the AuditWriteFailures metric filter in template.yaml) but
does not fail the business operation that triggered it.
"""
import json
import logging
import os
import uuid

from . import aws
from .ddb import now_iso, now_s, to_ddb, ttl_after_days

logger = logging.getLogger("audit")

EVENTS = {
    "UPLOAD_URL_ISSUED", "DOCUMENT_UPLOADED", "DOCUMENT_PROCESSED", "DOCUMENT_FAILED",
    "DOCUMENT_QUERIED", "DOCUMENT_COMPARED", "DOCUMENTS_SEARCHED", "PII_DETECTED", "PII_REDACTED",
    "RESULT_MODIFIED", "FIELD_VERIFIED", "EXPORT_GENERATED", "CHAT_SESSION_DELETED",
    "ANOMALY_REVIEWED", "PROCESSING_RETRIED", "VERSION_LINKED",
}
# keys that may carry document content or user-entered text -> always dropped
SENSITIVE_KEYS = {"text", "question", "answer", "value", "originalvalue", "correctedvalue", "content",
                  "query", "snippet", "quote", "note", "summary"}
MAX_STR = 120


def _sanitize(details):
    clean = {}
    for k, v in (details or {}).items():
        if k.lower() in SENSITIVE_KEYS:
            continue
        if isinstance(v, str):
            clean[k] = v[:MAX_STR]
        elif isinstance(v, (int, float, bool)) or v is None:
            clean[k] = v
        elif isinstance(v, (list, tuple)):
            clean[k] = [x[:MAX_STR] if isinstance(x, str) else x for x in v[:20]
                        if isinstance(x, (str, int, float, bool))]
        elif isinstance(v, dict):
            clean[k] = {str(dk)[:60]: dv for dk, dv in list(v.items())[:30] if isinstance(dv, (int, float, bool))}
    return clean


def record(event_type, doc_id=None, actor="system", details=None, counters=None, request_id=None):
    """Write an audit event. Returns the stored event (or None if it could not be stored)."""
    if event_type not in EVENTS:
        raise ValueError(f"unknown audit event {event_type}")
    ts = now_iso()
    event_id = uuid.uuid4().hex
    clean = _sanitize(details)
    event = {
        "pk": f"DOC#{doc_id}" if doc_id else "SYSTEM",
        "sk": f"AUDIT#{ts}#{event_id[:8]}",
        "gsi1pk": f"AUDIT#{ts[:10]}",
        "gsi1sk": f"{ts}#{event_id}",
        "type": "AUDIT",
        "eventId": event_id,
        "eventType": event_type,
        "docId": doc_id,
        "actor": (actor or "anonymous")[:80],
        "requestId": request_id,
        "timestamp": ts,
        "details": clean,
        "expiresAt": ttl_after_days(os.environ.get("AUDIT_TTL_DAYS", "365")),
    }
    # CloudWatch: structured, content-free line (queryable with Logs Insights: filter audit = 1)
    logger.info(json.dumps({"audit": 1, "eventType": event_type, "docId": doc_id, "actor": event["actor"],
                            "eventId": event_id, "details": clean}))
    try:
        aws.table("app").put_item(Item=to_ddb({k: v for k, v in event.items() if v is not None}))
        bump(event_type, counters)
        return event
    except Exception:
        logger.exception("audit write failed for %s", event_type)
        return None


def bump(event_type=None, counters=None):
    """Atomically increment dashboard counters on the STATS item."""
    adds = dict(counters or {})
    if event_type:
        adds[f"count_{event_type}"] = adds.get(f"count_{event_type}", 0) + 1
    adds = {k: v for k, v in adds.items() if v}
    if not adds:
        return
    names = {f"#c{i}": k for i, k in enumerate(adds)}
    values = {f":v{i}": int(v) for i, v in enumerate(adds.values())}
    aws.table("app").update_item(
        Key={"pk": "STATS", "sk": "COUNTERS"},
        UpdateExpression="ADD " + ", ".join(f"#c{i} :v{i}" for i in range(len(adds))) + " SET updatedAt = :now",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**values, ":now": now_s()},
    )
