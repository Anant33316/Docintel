"""Invoice fields (Feature A) and explainable anomaly findings with human review (Feature C).

* ``GET  /documents/{docId}/invoice``  parsed invoice fields with human corrections applied, the arithmetic
  checks and this document's findings.
* ``GET  /anomalies``                  findings across all documents (``?status=open|acknowledged|dismissed``,
  ``?severity=high|medium|low|info``).
* ``POST /documents/{docId}/anomalies/{findingId}/review``  ``{"action": "acknowledge"|"dismiss"|"reopen",
  "note": "..."}``. The decision is stored in the App table (``FINDING#``) and written to the audit trail.

Findings are recomputed on every request from the stored extraction, so they always reflect the current
data; only reviewers' decisions are persisted.
"""
from docintel_common import anomalies, audit, aws
from docintel_common.ddb import from_ddb, now_iso, query_all, scan_all, to_ddb
from docintel_common.http import ApiError, actor, body_of, path_param, query, request_id, resp

DOC_FIELDS = ("docId", "fileName", "status", "category", "invoice", "verifications", "versionGroup", "versionNumber",
              "createdAt")
ACTIONS = {"acknowledge": "acknowledged", "dismiss": "dismissed", "reopen": None}
MAX_NOTE = 500


def _all_docs():
    items, _ = scan_all(aws.table("docs"), max_items=5000,
                        ProjectionExpression=", ".join(f"#{i}" for i in range(len(DOC_FIELDS))),
                        ExpressionAttributeNames={f"#{i}": f for i, f in enumerate(DOC_FIELDS)})
    return [from_ddb(i) for i in items]


def _decisions(doc_ids):
    out = {}
    for doc_id in doc_ids:
        for r in query_all(aws.table("app"), max_items=500, KeyConditionExpression="pk = :p AND begins_with(sk, :f)",
                           ExpressionAttributeValues={":p": f"DOC#{doc_id}", ":f": "FINDING#"}):
            r = from_ddb(r)
            out[r["findingId"]] = {k: r.get(k) for k in ("decision", "note", "decidedBy", "decidedAt", "rule")}
    return out


def _findings(doc_id=None):
    docs = _all_docs()
    found = anomalies.detect(docs)
    ids = {f["docId"] for f in found if doc_id is None or f["docId"] == doc_id}
    decisions = _decisions(ids)
    for f in found:
        d = decisions.get(f["findingId"])
        f["review"], f["status"] = d, (d["decision"] if d else "open")
    return docs, [f for f in found if doc_id is None or f["docId"] == doc_id]


# ---------------------------------------------------------------- GET /documents/{docId}/invoice
def get_invoice(event):
    doc_id = path_param(event, "docId")
    item = aws.table("docs").get_item(Key={"docId": doc_id}).get("Item")
    if not item:
        raise ApiError(404, "document not found")
    item = from_ddb(item)
    if item.get("status") != "COMPLETED":
        raise ApiError(409, f"document is {item.get('status')}")
    inv = anomalies.invoice_view(item)
    if inv is None:
        return resp(200, {"docId": doc_id, "available": False,
                          "reason": "This document was processed before invoice parsing existed. Re-upload it."})
    _, found = _findings(doc_id)
    return resp(200, {"docId": doc_id, "available": True, "invoiceLike": anomalies.invoice.is_invoice_like(item, inv),
                      "method": inv.get("method"), "fields": inv["fields"], "checks": inv["checks"],
                      "findings": found, "rules": anomalies.RULES})


# ---------------------------------------------------------------- GET /anomalies
def list_anomalies(event):
    params = query(event)
    status, severity = params.get("status"), params.get("severity")
    if status and status not in ("open", "acknowledged", "dismissed"):
        raise ApiError(400, "status must be open, acknowledged or dismissed")
    if severity and severity not in anomalies.SEVERITIES:
        raise ApiError(400, f"severity must be one of {list(anomalies.SEVERITIES)}")
    docs, found = _findings()
    summary = anomalies.summarize(found)
    shown = [f for f in found if (not status or f["status"] == status) and (not severity or f["severity"] == severity)]
    checked = sum(1 for d in docs if d.get("status") == "COMPLETED" and anomalies.invoice_view(d)
                  and anomalies.invoice.is_invoice_like(d, anomalies.invoice_view(d)))
    return resp(200, {"documentsChecked": checked, "highValueThreshold": str(anomalies.high_value_threshold()),
                      "summary": summary, "findings": shown, "rules": anomalies.RULES})


# ---------------------------------------------------------------- POST .../anomalies/{findingId}/review
def review_finding(event):
    doc_id = path_param(event, "docId")
    finding_id = path_param(event, "findingId")
    body = body_of(event)
    user = actor(event, body)
    action = body.get("action")
    if action not in ACTIONS:
        raise ApiError(400, "action must be 'acknowledge', 'dismiss' or 'reopen'")
    note = (body.get("note") or "").strip()
    if len(note) > MAX_NOTE:
        raise ApiError(400, f"note must be at most {MAX_NOTE} characters")
    if action == "dismiss" and len(note) < 3:
        raise ApiError(400, "a short note explaining why the finding is dismissed is required")
    _, found = _findings(doc_id)
    finding = next((f for f in found if f["findingId"] == finding_id), None)
    if not finding:
        raise ApiError(404, "finding not found (it may no longer apply after a correction)")
    key = {"pk": f"DOC#{doc_id}", "sk": f"FINDING#{finding_id}"}
    table = aws.table("app")
    decision = ACTIONS[action]
    if decision is None:
        table.delete_item(Key=key)
        record = None
    else:
        record = {"decision": decision, "note": note or None, "decidedBy": user, "decidedAt": now_iso(),
                  "rule": finding["rule"]}
        table.put_item(Item=to_ddb({**key, "type": "FINDING_DECISION", "docId": doc_id, "findingId": finding_id,
                                    **{k: v for k, v in record.items() if v is not None}}))
    audit.record("ANOMALY_REVIEWED", doc_id, user, {"findingId": finding_id, "rule": finding["rule"],
                                                     "severity": finding["severity"], "action": action},
                 request_id=request_id(event))
    finding["review"], finding["status"] = record, (decision or "open")
    return resp(200, {"docId": doc_id, "finding": finding})
