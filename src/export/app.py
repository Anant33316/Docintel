"""Export processed results as JSON, CSV or a PDF report (Feature 10).

The file is written to ``exports/{docId}/`` (auto-expires after 7 days via the
bucket lifecycle rule) and a 5-minute presigned download URL is returned, which
avoids API Gateway's payload/binary limits. Human corrections are exported next
to - never instead of - the original AI values.
"""
import logging
import uuid

import formats
from docintel_common import anomalies, audit, aws, review, results
from docintel_common.ddb import from_ddb, now_iso
from docintel_common.http import ApiError, actor, dispatch, path_param, query, request_id, resp

logger = logging.getLogger()
logger.setLevel(logging.INFO)

URL_TTL = 300
FORMATS = {
    "json": ("application/json", formats.to_json),
    "csv": ("text/csv; charset=utf-8", formats.to_csv),
    "pdf": ("application/pdf", formats.to_pdf),
}


def handler(event, context):
    return dispatch(event, ROUTES)


def build_model(item, data, include_text, user):
    thr = review.threshold()
    fields = review.fields_view({**item, "fields": data["fields"] if not item.get("fields") else item["fields"]}, thr)
    pms = item.get("processingMs")
    # Offline (local demo) documents carry a ``provenance`` map; exports must not claim AWS AI produced them.
    prov = item.get("provenance") or {}
    stages = prov.get("stages") or {}
    generated_by = prov.get("notice") or "Amazon Bedrock / Comprehend"
    conf_source = (stages.get("confidence") or {}).get("label") or \
        "Amazon Textract (0-100); entity scores from Amazon Comprehend (0-1)"
    return {
        "exportVersion": "1.0",
        "generatedAt": now_iso(),
        "generatedBy": user,
        "document": {
            "docId": item["docId"], "fileName": item.get("fileName"), "status": item.get("status"),
            "language": item.get("language"), "pageCount": item.get("pageCount"),
            "charCount": item.get("charCount"), "tableCount": item.get("tableCount"),
            "createdAt": item.get("createdAt"), "completedAt": item.get("completedAt"),
            "processingSeconds": round(pms / 1000, 2) if isinstance(pms, (int, float)) else None,
            "avgOcrConfidence": item.get("avgOcrConfidence"),
        },
        "classification": {
            "category": item.get("category"),
            "method": item.get("classificationMethod") or "Amazon Bedrock LLM",
        },
        "aiInsights": {"summary": item.get("summary", ""), "intent": item.get("intent", ""),
                       "keyPhrases": item.get("keyPhrases", []), "generatedBy": generated_by},
        "entities": item.get("entities", []),
        "keyDates": item.get("keyDates", []),
        "actions": item.get("actions", []),
        "confidence": {"threshold": thr, "summary": review.summarize(fields, thr), "source": conf_source},
        "provenance": prov or None,
        # deterministic invoice fields (reviewers' corrections applied, originals kept) and arithmetic checks
        "invoice": anomalies.invoice_view(item),
        "fields": fields,
        "tables": data.get("tables", []),
        "piiSummary": item.get("piiSummary"),
        "aiStats": item.get("aiStats"),
        "extractedText": data.get("text", "") if include_text else None,
    }


def export(event):
    doc_id = path_param(event, "docId")
    params = query(event)
    fmt = (params.get("format") or "json").lower()
    if fmt not in FORMATS:
        raise ApiError(400, f"format must be one of {sorted(FORMATS)}")
    include_text = params.get("includeText", "true").lower() != "false"
    user = actor(event)

    item = aws.table("docs").get_item(Key={"docId": doc_id}).get("Item")
    if not item:
        raise ApiError(404, "document not found")
    item = from_ddb(item)
    try:
        data = results.load(item)
    except results.NotReady as e:
        raise ApiError(409, str(e))

    model = build_model(item, data, include_text, user)
    content_type, render = FORMATS[fmt]
    body = render(model)
    export_id = uuid.uuid4().hex[:16]
    key = f"exports/{doc_id}/{export_id}.{fmt}"
    s3 = aws.client("s3")
    base = (item.get("fileName") or doc_id).rsplit(".", 1)[0][:80]
    s3.put_object(Bucket=aws.bucket(), Key=key, Body=body, ContentType=content_type, ServerSideEncryption="AES256",
                  ContentDisposition=f'attachment; filename="{base}-docintel.{fmt}"')
    url = s3.generate_presigned_url("get_object", Params={"Bucket": aws.bucket(), "Key": key}, ExpiresIn=URL_TTL)
    audit.record("EXPORT_GENERATED", doc_id, user, {"format": fmt, "bytes": len(body), "exportId": export_id,
                                                    "includeText": include_text}, request_id=request_id(event))
    return resp(200, {"docId": doc_id, "exportId": export_id, "format": fmt, "bytes": len(body),
                      "objectKey": key, "downloadUrl": url, "urlExpiresInSeconds": URL_TTL})


ROUTES = {("GET", "/documents/{docId}/export"): export}
