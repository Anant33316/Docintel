"""Confidence scoring (Feature 3) and human-in-the-loop verification (Feature 7).

* ``fields`` (AI extraction) is immutable after processing.
* Human decisions go to ``verifications.{fieldId}`` on the document item.
* Every decision is also appended to the AppTable as an immutable ``VERIFY#`` record
  (original value, previous correction, new value, status, timestamp, user).
* Concurrent reviewers are serialised with an optimistic lock (``reviewVersion``).
"""
from docintel_common import audit, aws, review
from docintel_common.aws import ClientError, error_code
from docintel_common.ddb import from_ddb, now_iso, query_all, to_ddb
from docintel_common.http import ApiError, actor, body_of, path_param, query, request_id, resp

MAX_VALUE = 2000
ACTIONS = {"confirm": review.VERIFIED, "correct": review.CORRECTED}


def _load(doc_id, consistent=False):
    item = aws.table("docs").get_item(Key={"docId": doc_id}, ConsistentRead=consistent).get("Item")
    if not item:
        raise ApiError(404, "document not found")
    item = from_ddb(item)
    if item.get("status") != "COMPLETED":
        raise ApiError(409, f"document is {item.get('status')}")
    return item


def _threshold(event):
    try:
        return review.threshold(query(event).get("threshold"))
    except ValueError:
        raise ApiError(400, "threshold must be a number between 0 and 100")


# ---------------------------------------------------------------- GET /documents/{docId}/fields
def list_fields(event):
    doc_id = path_param(event, "docId")
    thr = _threshold(event)
    item = _load(doc_id)
    fields = review.fields_view(item, thr)
    status = query(event).get("status")
    if status == review.REQUIRES_REVIEW:
        shown = [f for f in fields if f["requiresVerification"]]
    elif status:
        shown = [f for f in fields if f["status"] == status]
    else:
        shown = fields
    return resp(200, {
        "docId": doc_id,
        "confidenceSource": "Amazon Textract (min of key and value block confidence, 0-100)",
        "summary": review.summarize(fields, thr),
        "fields": shown,
        "entities": item.get("entities", []),          # Comprehend scores (0-1)
        "avgOcrConfidence": item.get("avgOcrConfidence"),
    })


# ---------------------------------------------------------------- POST .../fields/{fieldId}/verify
def verify_field(event):
    doc_id = path_param(event, "docId")
    field_id = path_param(event, "fieldId")
    body = body_of(event)
    user = actor(event, body)
    action = body.get("action")
    if action not in ACTIONS:
        raise ApiError(400, "action must be 'confirm' or 'correct'")
    corrected = body.get("correctedValue")
    if action == "correct":
        if not isinstance(corrected, str) or not corrected.strip() or len(corrected) > MAX_VALUE:
            raise ApiError(400, f"correctedValue (1-{MAX_VALUE} chars) is required for action 'correct'")
        corrected = corrected.strip()
    else:
        corrected = None
    note = (body.get("note") or "")[:500] or None
    expected = body.get("expectedVersion")
    if expected is not None:
        try:
            expected = int(expected)
        except (TypeError, ValueError):
            raise ApiError(400, "expectedVersion must be an integer")

    for attempt in range(2):      # one retry if another reviewer updated the document concurrently
        item = _load(doc_id, consistent=True)
        thr = review.threshold()
        views = {f["fieldId"]: f for f in review.fields_view(item, thr)}
        field = views.get(field_id)
        if not field:
            raise ApiError(404, "field not found")
        prev = (item.get("verifications") or {}).get(field_id) or {}
        if expected is not None and expected != int(prev.get("version", 0)):
            raise ApiError(409, "field was modified by someone else; reload and retry")
        if action == "correct" and corrected == field["aiValue"]:
            action_status = review.VERIFIED      # "correcting" to the same value is a confirmation
            corrected = None
        else:
            action_status = ACTIONS[action]

        verification = {
            "fieldId": field_id,
            "status": action_status,
            "originalValue": field["aiValue"],
            "correctedValue": corrected,
            "verifiedAt": now_iso(),
            "verifiedBy": user,
            "note": note,
            "version": int(prev.get("version", 0)) + 1,
        }
        new_verifs = {**(item.get("verifications") or {}), field_id: verification}
        summary = review.summarize(review.fields_view({**item, "verifications": new_verifs}, thr), thr)
        old_rv = int(item.get("reviewVersion", 0))

        names = {"#v": "verifications"}
        values = {":rs": summary["reviewStatus"], ":sum": summary, ":nrv": old_rv + 1, ":orv": old_rv}
        if "verifications" in item:
            set_v = "#v.#f = :v"
            names["#f"] = field_id
            values[":v"] = verification
        else:                                    # documents processed before the upgrade
            set_v = "#v = :vmap"
            values[":vmap"] = {field_id: verification}
        try:
            aws.table("docs").update_item(
                Key={"docId": doc_id},
                UpdateExpression=f"SET {set_v}, reviewStatus = :rs, reviewSummary = :sum, reviewVersion = :nrv",
                ConditionExpression="attribute_not_exists(reviewVersion) OR reviewVersion = :orv",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=to_ddb(values),
            )
            break
        except ClientError as e:
            if error_code(e) != "ConditionalCheckFailedException":
                raise
            if attempt == 1:
                raise ApiError(409, "document is being reviewed concurrently; retry")

    # immutable audit-trail record (values are stored here on purpose: this IS the review record)
    aws.table("app").put_item(Item=to_ddb({
        "pk": f"DOC#{doc_id}",
        "sk": f"VERIFY#{verification['verifiedAt']}#{field_id}#v{verification['version']:04d}",
        "type": "VERIFICATION",
        "docId": doc_id,
        "fieldId": field_id,
        "fieldKey": field["key"],
        "aiConfidence": field["confidence"],
        "originalValue": field["aiValue"],
        "previousCorrectedValue": prev.get("correctedValue"),
        "previousStatus": prev.get("status") or field["aiStatus"],
        "correctedValue": corrected,
        "status": action_status,
        "verifiedAt": verification["verifiedAt"],
        "verifiedBy": user,
        "note": note,
        "version": verification["version"],
    }))
    audit.record("FIELD_VERIFIED", doc_id, user, {"fieldId": field_id, "status": action_status,
                                                    "version": verification["version"]},
                 request_id=request_id(event))
    if action_status == review.CORRECTED or prev.get("status") == review.CORRECTED:
        audit.record("RESULT_MODIFIED", doc_id, user, {"fieldId": field_id, "change": "field_correction",
                                                        "status": action_status}, request_id=request_id(event))
    original = {"fieldId": field_id, "key": field["key"], "value": field["aiValue"], "page": field["page"],
                "confidence": field["confidence"], "keyConfidence": field["keyConfidence"],
                "valueConfidence": field["valueConfidence"]}
    updated = review.field_view(original, verification, thr)
    return resp(200, {"docId": doc_id, "field": updated, "reviewSummary": summary})


# ---------------------------------------------------------------- GET /documents/{docId}/verifications
def list_verifications(event):
    doc_id = path_param(event, "docId")
    rows = query_all(aws.table("app"), max_items=1000, KeyConditionExpression="pk = :p AND begins_with(sk, :v)",
                     ExpressionAttributeValues={":p": f"DOC#{doc_id}", ":v": "VERIFY#"})
    field_id = query(event).get("fieldId")
    trail = [from_ddb({k: v for k, v in r.items() if k not in ("pk", "sk", "type")}) for r in rows
             if not field_id or r.get("fieldId") == field_id]
    return resp(200, {"docId": doc_id, "auditTrail": trail})
