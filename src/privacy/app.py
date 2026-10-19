"""PII detection and redaction (Feature 4) - isolated in its own Lambda so that only
this function holds comprehend:DetectPiiEntities and write access to ``redacted/``.

* Detection mode returns entity types, scores, locations and a *masked* preview;
  raw PII values are never returned, stored in DynamoDB, or logged.
* Redaction writes a NEW object under ``redacted/{docId}/{redactionId}/``; the
  original upload and the ``results/`` extraction are never modified.
"""
import hashlib
import json
import logging
import uuid

from docintel_common import audit, aws, pii, results
from docintel_common.ddb import from_ddb, now_iso, query_all, to_ddb
from docintel_common.http import ApiError, actor, body_of, dispatch, path_param, request_id, resp

logger = logging.getLogger()
logger.setLevel(logging.INFO)

URL_TTL = 300
MASKS = ("TYPE", "CHAR")


def handler(event, context):
    return dispatch(event, ROUTES)


def _load(doc_id):
    item = aws.table("docs").get_item(Key={"docId": doc_id}).get("Item")
    if not item:
        raise ApiError(404, "document not found")
    item = from_ddb(item)
    if item.get("status") != "COMPLETED":
        raise ApiError(409, f"document is {item.get('status')}")
    return item, results.load(item)


def _segments(data):
    """Flatten the extraction into single-line segments for one Comprehend pass.

    Form fields are sent as ``"key: value"`` so Comprehend sees the label as context
    (e.g. "Account Number: 12345678"); key and value are still redacted separately.
    Returns ``(segments, units)``; each unit is ``(kind, page, ref, segment_index, start, end)``
    with start/end relative to its segment.
    """
    segs, units = [], []
    for page in data["pageLines"]:
        for i, ln in enumerate(page["lines"]):
            t = ln.get("text") or ""
            units.append(("line", page.get("page"), i, len(segs), 0, len(t)))
            segs.append(t)
    for f in data["fields"]:
        k, v = f.get("key") or "", f.get("value") or ""
        units.append(("fieldKey", f.get("page"), f["fieldId"], len(segs), 0, len(k)))
        units.append(("fieldValue", f.get("page"), f["fieldId"], len(segs), len(k) + 2, len(k) + 2 + len(v)))
        segs.append(f"{k}: {v}")
    for t, table in enumerate(data.get("tables") or []):
        for r, row in enumerate(table):
            for c, cell in enumerate(row):
                units.append(("tableCell", None, f"{t}:{r}:{c}", len(segs), 0, len(cell or "")))
                segs.append(cell or "")
    return segs, units


def _options(body):
    try:
        min_score = float(body.get("minScore", 0.5))
    except (TypeError, ValueError):
        raise ApiError(400, "minScore must be a number")
    if not 0 <= min_score <= 1:
        raise ApiError(400, "minScore must be between 0 and 1")
    types = body.get("entityTypes") or None
    if types is not None and (not isinstance(types, list) or not all(isinstance(t, str) for t in types)):
        raise ApiError(400, "entityTypes must be a list of Comprehend PII types, e.g. [\"NAME\", \"EMAIL\"]")
    return min_score, [t.upper() for t in types] if types else None


def _detect(item, data):
    segs, units = _segments(data)
    joined, spans = pii.join_segments(segs)
    entities, lang = pii.detect(joined, item.get("language", "en"), min_score=0.0)
    ranges = [(spans[i][0] + s, spans[i][0] + e) for _, _, _, i, s, e in units]
    return joined, units, ranges, entities, lang


def _locate(units, ranges, offset):
    for unit, (s, e) in zip(units, ranges):
        if s <= offset < max(e, s + 1):
            return unit
    for unit, (s, e) in zip(units, ranges):          # e.g. the ": " between a field key and value
        if unit[0] == "fieldKey" and s <= offset <= e + 2:
            return ("field",) + unit[1:]
    return ("unknown", None, None)


# ---------------------------------------------------------------- POST /documents/{docId}/pii
def detect_pii(event):
    doc_id = path_param(event, "docId")
    body = body_of(event)
    min_score, types = _options(body)
    item, data = _load(doc_id)
    joined, units, ranges, entities, lang = _detect(item, data)
    entities = [e for e in entities if e["score"] >= min_score and (not types or e["type"] in types)]

    found, counts = [], {}
    for e in entities:
        kind, page, ref = _locate(units, ranges, e["beginOffset"])[:3]
        counts[e["type"]] = counts.get(e["type"], 0) + 1
        found.append({"type": e["type"], "score": e["score"], "location": kind, "page": page, "ref": ref,
                      "preview": pii.mask_preview(joined[e["beginOffset"]:e["endOffset"]])})
    summary = {"scannedAt": now_iso(), "languageCode": lang, "minScore": min_score,
               "total": len(found), "counts": counts}
    aws.table("docs").update_item(Key={"docId": doc_id}, UpdateExpression="SET piiSummary = :p",
                                  ExpressionAttributeValues={":p": to_ddb(summary)})
    audit.record("PII_DETECTED", doc_id, actor(event, body), {"total": len(found), "types": sorted(counts)},
                 request_id=request_id(event))
    warn = None if item.get("language", "en") in pii.PII_LANGS else \
        f"Document language '{item.get('language')}' is not supported by Comprehend PII; scanned as English."
    return resp(200, {"docId": doc_id, "mode": "detect", **summary, "entities": found[:500], "warning": warn})


# ---------------------------------------------------------------- POST /documents/{docId}/redact
def redact(event):
    doc_id = path_param(event, "docId")
    body = body_of(event)
    user = actor(event, body)
    min_score, types = _options(body)
    mask = (body.get("mask") or "TYPE").upper()
    if mask not in MASKS:
        raise ApiError(400, f"mask must be one of {MASKS}")
    item, data = _load(doc_id)
    joined, units, ranges, entities, lang = _detect(item, data)
    red, counts = pii.redact_ranges(joined, ranges, entities, entity_types=types, min_score=min_score, mask=mask)

    # rebuild a redacted copy of the extraction with the same shape as results/{docId}.json
    lines_by_page, fields, tables = {}, {}, [[list(r) for r in t] for t in data.get("tables") or []]
    for value, (kind, page, ref, *_rest) in zip(red, units):
        if kind == "line":
            lines_by_page.setdefault(page, []).append(value)
        elif kind == "fieldKey":
            fields.setdefault(ref, {})["key"] = value
        elif kind == "fieldValue":
            fields.setdefault(ref, {})["value"] = value
        elif kind == "tableCell":
            t, r, c = (int(x) for x in ref.split(":"))
            tables[t][r][c] = value
    page_lines = [{"page": p["page"], "lines": [{"text": t} for t in lines_by_page.get(p["page"], [])]}
                  for p in data["pageLines"]]
    red_fields = [{"fieldId": f["fieldId"], "key": fields[f["fieldId"]]["key"],
                   "value": fields[f["fieldId"]]["value"], "confidence": f.get("confidence"), "page": f.get("page")}
                  for f in data["fields"]]
    text = "\n".join(t for p in page_lines for t in (ln["text"] for ln in p["lines"]))

    rid = uuid.uuid4().hex[:16]
    prefix = f"redacted/{doc_id}/{rid}"
    created = now_iso()
    meta = {"docId": doc_id, "redactionId": rid, "createdAt": created, "requestedBy": user,
            "entityTypes": types or ["ALL"], "minScore": min_score, "mask": mask, "languageCode": lang,
            "counts": counts, "totalRedacted": sum(counts.values()), "sourceResultKey": item["resultKey"],
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "objects": {"json": f"{prefix}/redacted.json", "text": f"{prefix}/redacted.txt"}}
    s3 = aws.client("s3")
    s3.put_object(Bucket=aws.bucket(), Key=meta["objects"]["json"], ContentType="application/json",
                  ServerSideEncryption="AES256",
                  Body=json.dumps({**meta, "text": text, "pageLines": page_lines, "fields": red_fields,
                                   "tables": tables}).encode())
    s3.put_object(Bucket=aws.bucket(), Key=meta["objects"]["text"], ContentType="text/plain; charset=utf-8",
                  ServerSideEncryption="AES256", Body=text.encode())
    aws.table("app").put_item(Item=to_ddb({"pk": f"DOC#{doc_id}", "sk": f"REDACT#{created}#{rid}",
                                           "type": "REDACTION", **meta}))
    audit.record("PII_REDACTED", doc_id, user, {"redactionId": rid, "totalRedacted": meta["totalRedacted"],
                                                "types": sorted(counts), "mask": mask},
                 request_id=request_id(event))
    return resp(200, {**meta, "downloads": _urls(meta["objects"]), "urlExpiresInSeconds": URL_TTL})


def _urls(objects):
    s3 = aws.client("s3")
    return {k: s3.generate_presigned_url("get_object", Params={"Bucket": aws.bucket(), "Key": v},
                                         ExpiresIn=URL_TTL) for k, v in objects.items()}


def list_redactions(event):
    doc_id = path_param(event, "docId")
    rows = query_all(aws.table("app"), max_items=200, KeyConditionExpression="pk = :p AND begins_with(sk, :r)",
                     ExpressionAttributeValues={":p": f"DOC#{doc_id}", ":r": "REDACT#"}, ScanIndexForward=False)
    out = []
    for r in rows:
        r = from_ddb({k: v for k, v in r.items() if k not in ("pk", "sk", "type")})
        r["downloads"] = _urls(r.get("objects") or {})
        out.append(r)
    return resp(200, {"docId": doc_id, "redactions": out, "urlExpiresInSeconds": URL_TTL})


ROUTES = {
    ("POST", "/documents/{docId}/pii"): detect_pii,
    ("POST", "/documents/{docId}/redact"): redact,
    ("GET", "/documents/{docId}/redactions"): list_redactions,
}
