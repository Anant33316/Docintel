"""Confidence scoring and human-in-the-loop review state for extracted fields.

The original AI extraction (``item["fields"]``) is never modified. Human
decisions live in ``item["verifications"][fieldId]`` and every decision is also
appended to an immutable audit trail (AppTable ``VERIFY#`` records).
"""
import os

REQUIRES_REVIEW = "REQUIRES_REVIEW"
AUTO_ACCEPTED = "AUTO_ACCEPTED"
VERIFIED = "VERIFIED"        # human confirmed the AI value
CORRECTED = "CORRECTED"      # human supplied a different value
UNKNOWN = "NO_CONFIDENCE"    # legacy data without confidence scores


def threshold(override=None):
    """Confidence threshold in percent (0-100). Configurable via CONFIDENCE_THRESHOLD."""
    value = override if override not in (None, "") else os.environ.get("CONFIDENCE_THRESHOLD", "90")
    t = float(value)
    if not 0 <= t <= 100:
        raise ValueError("threshold must be between 0 and 100")
    return t


def initial_status(confidence, thr):
    if confidence is None:
        return UNKNOWN
    return REQUIRES_REVIEW if confidence < thr else AUTO_ACCEPTED


def field_view(field, verification, thr):
    conf = field.get("confidence")
    ai_status = initial_status(conf, thr)
    v = verification or {}
    status = v.get("status") or ai_status
    effective = v.get("correctedValue") if v.get("status") == CORRECTED else field.get("value")
    return {
        "fieldId": field["fieldId"],
        "key": field.get("key"),
        "page": field.get("page"),
        "aiValue": field.get("value"),                 # original AI extraction, never overwritten
        "confidence": conf,
        "keyConfidence": field.get("keyConfidence"),
        "valueConfidence": field.get("valueConfidence"),
        "requiresVerification": ai_status in (REQUIRES_REVIEW, UNKNOWN) and not v.get("status"),
        "aiStatus": ai_status,
        "status": status,
        "correctedValue": v.get("correctedValue"),
        "effectiveValue": effective,
        "verifiedAt": v.get("verifiedAt"),
        "verifiedBy": v.get("verifiedBy"),
        "version": v.get("version", 0),
        # where the value came from: "textract" (OCR key-value with confidence) or "deterministic-parser"
        "source": field.get("source") or "textract",
        "parseStatus": field.get("parseStatus"),
        "evidence": field.get("evidence"),
    }


def fields_view(item, thr):
    verifications = item.get("verifications") or {}
    fields = [field_view(f, verifications.get(f["fieldId"]), thr) for f in item.get("fields") or []]
    if not item.get("fields") and item.get("keyValues"):
        # documents processed before the upgrade only stored key->value without confidence
        fields = [field_view({"fieldId": f"legacy{i:03d}", "key": k, "value": v, "confidence": None},
                             verifications.get(f"legacy{i:03d}"), thr)
                  for i, (k, v) in enumerate(item["keyValues"].items())]
    return fields


def summarize(fields, thr):
    pending = sum(1 for f in fields if f["requiresVerification"])
    return {
        "threshold": thr,
        "totalFields": len(fields),
        "lowConfidence": sum(1 for f in fields if f["aiStatus"] == REQUIRES_REVIEW),
        "pendingReview": pending,
        "verified": sum(1 for f in fields if f["status"] == VERIFIED),
        "corrected": sum(1 for f in fields if f["status"] == CORRECTED),
        "reviewStatus": "PENDING_REVIEW" if pending else ("REVIEWED" if any(
            f["status"] in (VERIFIED, CORRECTED) for f in fields) else "NOT_REQUIRED"),
    }
