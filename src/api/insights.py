"""Analytics dashboard data (Feature 6) and audit-trail queries (Feature 11).

``/stats`` is computed from the existing documents table plus the atomic
counters that ``audit.record`` maintains, inside the existing API Lambda (no
extra analytics service).
"""
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from docintel_common import aws
from docintel_common.ddb import from_ddb, query_all, scan_all
from docintel_common.http import ApiError, path_param, query, resp

STATS_FIELDS = ("docId", "fileName", "status", "category", "createdAt", "startedAt", "completedAt", "failedAt",
                "processingMs", "errorMessage", "aiStats", "reviewStatus", "reviewSummary", "pageCount",
                "keyDates", "avgOcrConfidence")


def _pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def stats(event):
    items, truncated = scan_all(aws.table("docs"), max_items=10000,
                                ProjectionExpression=", ".join(f"#{i}" for i in range(len(STATS_FIELDS))),
                                ExpressionAttributeNames={f"#{i}": f for i, f in enumerate(STATS_FIELDS)})
    docs = [from_ddb(i) for i in items]
    by_status = Counter(d.get("status", "UNKNOWN") for d in docs)
    categories = Counter(d.get("category") for d in docs if d.get("status") == "COMPLETED")

    durations = []
    for d in docs:
        if d.get("status") != "COMPLETED":
            continue
        if d.get("processingMs") is not None:
            durations.append(d["processingMs"] / 1000)
        elif d.get("completedAt") and d.get("startedAt"):
            durations.append(d["completedAt"] - d["startedAt"])

    failures = sorted((d for d in docs if d.get("status") == "FAILED"),
                      key=lambda d: d.get("failedAt") or d.get("createdAt", 0), reverse=True)
    recent = sorted(docs, key=lambda d: d.get("createdAt", 0), reverse=True)

    ai = Counter()
    for d in docs:
        for k, v in (d.get("aiStats") or {}).items():
            if isinstance(v, (int, float)):
                ai[k] += v
    counters = from_ddb(aws.table("app").get_item(Key={"pk": "STATS", "sk": "COUNTERS"}).get("Item") or {})
    events = {k[len("count_"):]: v for k, v in counters.items() if k.startswith("count_")}

    today = datetime.now(timezone.utc).date()
    per_day = Counter(datetime.fromtimestamp(d["createdAt"], timezone.utc).date().isoformat()
                      for d in docs if d.get("createdAt"))
    uploads_per_day = [{"date": (today - timedelta(days=i)).isoformat(),
                        "count": per_day.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)]

    review_pending = sum(1 for d in docs if d.get("reviewStatus") == "PENDING_REVIEW")
    low_fields = sum((d.get("reviewSummary") or {}).get("lowConfidence", 0) for d in docs)
    ocr = [d["avgOcrConfidence"] for d in docs if d.get("avgOcrConfidence") is not None]
    upcoming = []
    for d in docs:
        for kd in d.get("keyDates") or []:
            if kd.get("isoDate") and kd["isoDate"] >= today.isoformat():
                upcoming.append({"docId": d["docId"], "fileName": d.get("fileName"), "type": kd.get("type"),
                                 "date": kd["isoDate"], "description": kd.get("description")})
    upcoming.sort(key=lambda x: x["date"])

    return resp(200, {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sampled": truncated,
        "documents": {
            "total": len(docs),
            "completed": by_status.get("COMPLETED", 0),
            "failed": by_status.get("FAILED", 0),
            "processing": by_status.get("PROCESSING", 0),
            "byStatus": dict(by_status),
        },
        "categories": dict(categories.most_common()),
        "processingTimeSeconds": {
            "average": round(sum(durations) / len(durations), 2) if durations else None,
            "p50": _pct(durations, 50), "p90": _pct(durations, 90), "max": max(durations) if durations else None,
            "samples": len(durations),
        },
        "failures": {
            "total": len(failures),
            "recent": [{"docId": d["docId"], "fileName": d.get("fileName"), "error": (d.get("errorMessage") or "")[:200],
                        "at": d.get("failedAt") or d.get("createdAt")} for d in failures[:10]],
        },
        "recentUploads": [{k: d.get(k) for k in ("docId", "fileName", "status", "category", "createdAt")}
                          for d in recent[:10]],
        "uploadsPerDay": uploads_per_day,
        "ai": {
            "documentsAnalyzed": by_status.get("COMPLETED", 0),
            "textractPages": ai.get("textractPages", 0),
            "comprehendCalls": ai.get("comprehendCalls", 0),
            "embeddingCalls": counters.get("embeddingCalls", ai.get("embeddingCalls", 0)),
            "bedrockCallsDuringProcessing": ai.get("bedrockCalls", 0),
            "bedrockInputTokens": counters.get("bedrockInputTokens", 0),
            "bedrockOutputTokens": counters.get("bedrockOutputTokens", 0),
            "questionsAnswered": events.get("DOCUMENT_QUERIED", 0),
            "comparisons": events.get("DOCUMENT_COMPARED", 0) // 2,   # one audit event per compared document
            "searches": events.get("DOCUMENTS_SEARCHED", 0),
            "piiScans": events.get("PII_DETECTED", 0),
            "redactions": events.get("PII_REDACTED", 0),
            "exports": events.get("EXPORT_GENERATED", 0),
            "fieldVerifications": events.get("FIELD_VERIFIED", 0),
            "averageOcrConfidence": round(sum(ocr) / len(ocr), 2) if ocr else None,
        },
        "review": {"documentsPendingReview": review_pending, "lowConfidenceFields": low_fields},
        "upcomingKeyDates": upcoming[:15],
        "eventCounts": events,
    })


def _audit_rows(rows):
    keep = ("eventId", "eventType", "docId", "actor", "timestamp", "details", "requestId")
    return [from_ddb({k: r.get(k) for k in keep if k in r}) for r in rows]


def doc_audit(event):
    doc_id = path_param(event, "docId")
    rows = query_all(aws.table("app"), max_items=500, KeyConditionExpression="pk = :p AND begins_with(sk, :a)",
                     ExpressionAttributeValues={":p": f"DOC#{doc_id}", ":a": "AUDIT#"}, ScanIndexForward=False)
    return resp(200, {"docId": doc_id, "events": _audit_rows(rows)})


def audit_by_day(event):
    params = query(event)
    day = params.get("date") or datetime.now(timezone.utc).date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ApiError(400, "date must be YYYY-MM-DD")
    event_type = params.get("eventType")
    rows = query_all(aws.table("app"), max_items=1000, IndexName="gsi1",
                     KeyConditionExpression="gsi1pk = :p", ExpressionAttributeValues={":p": f"AUDIT#{day}"},
                     ScanIndexForward=False)
    events = [e for e in _audit_rows(rows) if not event_type or e.get("eventType") == event_type]
    return resp(200, {"date": day, "events": events})
