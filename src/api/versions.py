"""Document version history (Feature E).

``POST /documents/{docId}/versions  {"previousDocId": "..."}`` records that ``docId`` revises an earlier
document. Nothing is copied or overwritten: both documents stay as they are; the link is two attributes on
the Documents items (``versionGroup`` = id of the first version, ``versionNumber``).

``GET /documents/{docId}/versions`` returns the timeline and, for each consecutive pair, the deterministic
differences: invoice fields (added / removed / changed, using reviewers' corrections when present) and a
line-level text diff (difflib, the same code as ``/compare``). These are detected differences only; what a
change *means* for the business is left to the reader (or to the AI interpretation in ``/compare`` on AWS).
"""
from docintel_common import anomalies, audit, aws, diffing, invoice, results
from docintel_common.aws import ClientError, error_code
from docintel_common.ddb import from_ddb, scan_all
from docintel_common.http import ApiError, actor, body_of, path_param, request_id, resp

FIELDS = ("docId", "fileName", "status", "createdAt", "completedAt", "versionGroup", "versionNumber", "versionOf",
          "invoice", "verifications", "resultKey", "pageCount", "category")
MAX_LINES = 20


def _load(doc_id):
    item = aws.table("docs").get_item(Key={"docId": doc_id}).get("Item")
    if not item:
        raise ApiError(404, f"document {doc_id} not found")
    return from_ddb(item)


def _group(group_id):
    items, _ = scan_all(aws.table("docs"), max_items=5000,
                        ProjectionExpression=", ".join(f"#{i}" for i in range(len(FIELDS))),
                        ExpressionAttributeNames={f"#{i}": f for i, f in enumerate(FIELDS)})
    docs = [from_ddb(i) for i in items if i.get("versionGroup") == group_id]
    return sorted(docs, key=lambda d: d.get("versionNumber") or 0)


# ---------------------------------------------------------------- POST /documents/{docId}/versions
def link_version(event):
    doc_id = path_param(event, "docId")
    body = body_of(event)
    prev_id = body.get("previousDocId")
    if not isinstance(prev_id, str) or not prev_id.isalnum() or len(prev_id) > 64:
        raise ApiError(400, "previousDocId is required")
    if prev_id == doc_id:
        raise ApiError(400, "a document cannot be a version of itself")
    user = actor(event, body)
    new, prev = _load(doc_id), _load(prev_id)
    for d in (new, prev):
        if d.get("status") != "COMPLETED":
            raise ApiError(409, f"{d.get('fileName') or d['docId']} is {d.get('status')}; only processed documents "
                                "can be linked as versions")
    if new.get("versionGroup"):
        raise ApiError(409, f"{new.get('fileName')} is already part of a version history")
    group = prev.get("versionGroup") or prev["docId"]
    members = _group(group) if prev.get("versionGroup") else []
    number = max([m.get("versionNumber") or 0 for m in members] + [1]) + 1
    table = aws.table("docs")
    try:
        if not prev.get("versionGroup"):
            table.update_item(Key={"docId": prev_id}, UpdateExpression="SET versionGroup = :g, versionNumber = :one",
                              ConditionExpression="attribute_not_exists(versionGroup)",
                              ExpressionAttributeValues={":g": group, ":one": 1})
        table.update_item(Key={"docId": doc_id},
                          UpdateExpression="SET versionGroup = :g, versionNumber = :n, versionOf = :p",
                          ConditionExpression="attribute_not_exists(versionGroup)",
                          ExpressionAttributeValues={":g": group, ":n": number, ":p": prev_id})
    except ClientError as e:
        if error_code(e) == "ConditionalCheckFailedException":
            raise ApiError(409, "the version history changed at the same time; reload and retry")
        raise
    audit.record("VERSION_LINKED", doc_id, user, {"versionGroup": group, "versionNumber": number,
                                                  "previousDocId": prev_id}, request_id=request_id(event))
    return resp(200, {"docId": doc_id, "versionGroup": group, "versionNumber": number, "versionOf": prev_id})


# ---------------------------------------------------------------- GET /documents/{docId}/versions
def _field_diff(a, b):
    fa = (anomalies.invoice_view(a) or {}).get("fields") or {}
    fb = (anomalies.invoice_view(b) or {}).get("fields") or {}
    out = {"added": [], "removed": [], "changed": [], "unchanged": []}
    for name in invoice.FIELD_ORDER:
        x, y = fa.get(name) or {}, fb.get(name) or {}
        xv = x.get("value") if x.get("status") in invoice.USABLE else None
        yv = y.get("value") if y.get("status") in invoice.USABLE else None
        row = {"field": name, "label": invoice.FIELD_LABELS[name], "before": x.get("raw"), "after": y.get("raw"),
               "pageBefore": x.get("page"), "pageAfter": y.get("page")}
        if xv is None and yv is None:
            continue
        if xv is None:
            out["added"].append(row)
        elif yv is None:
            out["removed"].append(row)
        elif xv != yv:
            out["changed"].append({**row, "normalizedBefore": xv, "normalizedAfter": yv})
        else:
            out["unchanged"].append(row)
    return out


def _text_diff(a, b):
    try:
        ta, tb = results.load(a).get("text", ""), results.load(b).get("text", "")
    except results.NotReady:
        return None
    d = diffing.diff_lines(ta, tb)
    return {k: d[k] for k in ("addedCount", "removedCount", "changedCount", "lineSimilarity")} | {
        "added": d["added"][:MAX_LINES], "removed": d["removed"][:MAX_LINES], "changed": d["changed"][:MAX_LINES]}


def _summary(d):
    f = (anomalies.invoice_view(d) or {}).get("fields") or {}

    def val(n):
        return (f.get(n) or {}).get("raw") if (f.get(n) or {}).get("status") in invoice.USABLE else None
    return {"docId": d["docId"], "fileName": d.get("fileName"), "status": d.get("status"),
            "createdAt": d.get("createdAt"), "versionNumber": d.get("versionNumber"), "versionOf": d.get("versionOf"),
            "pageCount": d.get("pageCount"), "invoiceNumber": val("invoiceNumber"), "invoiceDate": val("invoiceDate"),
            "total": val("total")}


def list_versions(event):
    doc_id = path_param(event, "docId")
    item = _load(doc_id)
    if not item.get("versionGroup"):
        return resp(200, {"docId": doc_id, "versionGroup": None, "versions": [_summary(item)], "changes": []})
    docs = _group(item["versionGroup"])
    changes = []
    for a, b in zip(docs, docs[1:]):
        fd = _field_diff(a, b)
        td = _text_diff(a, b)
        identical = td is not None and not (td["addedCount"] or td["removedCount"] or td["changedCount"])
        changes.append({"from": a["docId"], "to": b["docId"], "fromVersion": a.get("versionNumber"),
                        "toVersion": b.get("versionNumber"), "fields": fd, "text": td, "identicalText": identical,
                        "method": "deterministic (field comparison + difflib line diff)"})
    return resp(200, {"docId": doc_id, "versionGroup": item["versionGroup"], "versions": [_summary(d) for d in docs],
                      "changes": changes})
