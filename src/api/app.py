"""REST API: upload URLs, list/get documents, grounded Q&A + chat, field review,
comparison, cross-document search, dashboard statistics and audit trail.

The original four routes keep their request/response contracts; new fields are
only ever added to responses.
"""
import logging
import os
import re
import uuid

from docintel_common import audit, aws
from docintel_common.http import ApiError, actor, body_of, dispatch, path_param, request_id, resp  # noqa: F401

import comparison
import insights
import invoices
import qa
import review_api
import search
import versions

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALLOWED = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".tif": "image/tiff", ".tiff": "image/tiff"}
HIDDEN = {"bucket", "key", "resultKey", "startedAt", "reviewVersion"}


def public(item):
    return {k: v for k, v in item.items() if k not in HIDDEN}


def handler(event, context):
    return dispatch(event, ROUTES)


# ---------------------------------------------------------------- original routes
def upload_url(event):
    body = body_of(event)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", body.get("fileName", ""))[-100:]
    ext = os.path.splitext(name.lower())[1]
    if ext not in ALLOWED:
        return resp(400, {"error": f"fileName must end with one of {sorted(ALLOWED)}"})
    key = f"uploads/{uuid.uuid4().hex}/{name}"
    url = aws.client("s3").generate_presigned_url(
        "put_object",
        Params={"Bucket": aws.bucket(), "Key": key, "ContentType": ALLOWED[ext]},
        ExpiresIn=300,
    )
    audit.record("UPLOAD_URL_ISSUED", None, actor(event, body), {"extension": ext}, request_id=request_id(event))
    return resp(200, {"uploadUrl": url, "key": key, "contentType": ALLOWED[ext],
                      "note": "PUT the file to uploadUrl with the same Content-Type header"})


def list_docs(event):
    items = aws.table("docs").scan(Limit=100).get("Items", [])   # fine at single-developer scale
    items.sort(key=lambda i: i.get("createdAt", 0), reverse=True)
    keys = ("docId", "fileName", "status", "category", "createdAt", "completedAt", "reviewStatus", "pageCount",
            "versionGroup", "versionNumber")
    slim = [{k: i.get(k) for k in keys} for i in items[:50]]
    return resp(200, {"documents": slim})


def get_doc(event):
    doc_id = path_param(event, "docId")
    item = aws.table("docs").get_item(Key={"docId": doc_id}).get("Item")
    return resp(200, public(item)) if item else resp(404, {"error": "document not found"})


ROUTES = {
    ("POST", "/documents/upload-url"): upload_url,
    ("GET", "/documents"): list_docs,
    ("GET", "/documents/{docId}"): get_doc,
    ("POST", "/documents/{docId}/ask"): qa.ask,                       # upgraded: now source-grounded
    # Feature 2 / 9: conversational, source-grounded Q&A
    ("POST", "/documents/{docId}/chat"): qa.chat,
    ("GET", "/documents/{docId}/chat"): qa.list_sessions,
    ("GET", "/documents/{docId}/chat/{sessionId}"): qa.get_session,
    ("DELETE", "/documents/{docId}/chat/{sessionId}"): qa.delete_session,
    # Feature 3 / 7: confidence + human verification
    ("GET", "/documents/{docId}/fields"): review_api.list_fields,
    ("POST", "/documents/{docId}/fields/{fieldId}/verify"): review_api.verify_field,
    ("GET", "/documents/{docId}/verifications"): review_api.list_verifications,
    # Feature 1: comparison
    ("POST", "/compare"): comparison.compare,
    ("GET", "/comparisons/{comparisonId}"): comparison.get_comparison,
    ("GET", "/documents/{docId}/comparisons"): comparison.list_for_doc,
    # Feature 5: search
    ("GET", "/search"): search.search,
    # Feature 6 / 11: dashboard + audit
    ("GET", "/stats"): insights.stats,
    ("GET", "/documents/{docId}/audit"): insights.doc_audit,
    ("GET", "/audit"): insights.audit_by_day,
    # Feature A / C: deterministic invoice fields, explainable anomaly findings + human review
    ("GET", "/documents/{docId}/invoice"): invoices.get_invoice,
    ("GET", "/anomalies"): invoices.list_anomalies,
    ("POST", "/documents/{docId}/anomalies/{findingId}/review"): invoices.review_finding,
    # Feature E: version history (links documents; never modifies their content)
    ("GET", "/documents/{docId}/versions"): versions.list_versions,
    ("POST", "/documents/{docId}/versions"): versions.link_version,
}
