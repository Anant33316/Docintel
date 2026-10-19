"""PII detection/redaction and export tests (processor + handlers + fakes)."""
import csv
import io
import json
import logging
import re
import zlib

import pytest
from helpers import INVOICE_A, call, ingest, load_app

DOC = "/documents/{docId}"


@pytest.fixture
def doc(env):
    return ingest(env, load_app("processor"), "uploads/aaa/invoice-a.png", INVOICE_A)


# ---------------------------------------------------------------- Feature 4: PII
def test_pii_detection_returns_masked_previews_only(env, doc):
    priv = load_app("privacy")
    code, r = call(priv, "POST", f"{DOC}/pii", {"docId": doc}, {"minScore": 0.5})
    assert code == 200 and r["mode"] == "detect"
    assert r["counts"]["NAME"] >= 1 and r["counts"]["EMAIL"] == 1 and r["counts"]["BANK_ACCOUNT_NUMBER"] == 1
    name = next(e for e in r["entities"] if e["type"] == "NAME")
    assert name["preview"] == "J*** S****" and name["page"] == 2
    raw = json.dumps(r)
    assert "John Smith" not in raw and "john.smith@example.com" not in raw and "12345678" not in raw
    item = env.docs.items[(doc,)]
    assert item["piiSummary"]["counts"]["EMAIL"] == 1 and "John" not in json.dumps(item["piiSummary"], default=str)
    assert env.audit_events("PII_DETECTED")


def test_entity_type_filter(env, doc):
    code, r = call(load_app("privacy"), "POST", f"{DOC}/pii", {"docId": doc}, {"entityTypes": ["EMAIL"]})
    assert set(r["counts"]) == {"EMAIL"}
    assert call(load_app("privacy"), "POST", f"{DOC}/pii", {"docId": doc}, {"minScore": 7})[0] == 400


def test_redaction_writes_new_object_and_never_touches_original(env, doc):
    priv = load_app("privacy")
    result_key = env.docs.items[(doc,)]["resultKey"]
    original = env.s3.objects[("bucket", result_key)]["Body"]
    keys_before = set(env.s3.objects)

    code, r = call(priv, "POST", f"{DOC}/redact", {"docId": doc}, {"entityTypes": ["NAME", "EMAIL", "BANK_ACCOUNT_NUMBER"]},
                   headers={"X-User-Id": "dpo"})
    assert code == 200 and r["totalRedacted"] >= 3 and r["requestedBy"] == "dpo"
    assert env.s3.objects[("bucket", result_key)]["Body"] == original           # original untouched
    new_keys = set(env.s3.objects) - keys_before
    assert new_keys == {("bucket", r["objects"]["json"]), ("bucket", r["objects"]["text"])}
    assert all(k[1].startswith(f"redacted/{doc}/") for k in new_keys)

    red = json.loads(env.s3.objects[("bucket", r["objects"]["json"])]["Body"])
    text = env.s3.objects[("bucket", r["objects"]["text"])]["Body"].decode()
    assert "John Smith" not in text and "[NAME]" in text and "[EMAIL]" in text
    assert "Total amount due: 40 USD" in text                                    # non-PII preserved
    acct = next(f for f in red["fields"] if f["key"] == "Account Number")
    assert acct["value"] == "[BANK_ACCOUNT_NUMBER]"
    assert red["sha256"] == r["sha256"] and r["downloads"]["json"].startswith("https://")

    meta = next(i for i in env.app.items.values() if i.get("type") == "REDACTION")
    assert meta["counts"]["NAME"] >= 1 and "text" not in meta
    code, listing = call(priv, "GET", f"{DOC}/redactions", {"docId": doc})
    assert listing["redactions"][0]["redactionId"] == r["redactionId"] and listing["redactions"][0]["downloads"]
    assert env.audit_events("PII_REDACTED")[0]["details"]["mask"] == "TYPE"


def test_char_mask_and_bad_mask(env, doc):
    priv = load_app("privacy")
    code, r = call(priv, "POST", f"{DOC}/redact", {"docId": doc}, {"mask": "char", "entityTypes": ["EMAIL"]})
    text = env.s3.objects[("bucket", r["objects"]["text"])]["Body"].decode()
    assert "Email: " + "*" * len("john.smith@example.com") in text
    assert call(priv, "POST", f"{DOC}/redact", {"docId": doc}, {"mask": "BLUR"})[0] == 400


def test_pii_values_never_logged(env, doc):
    records = []

    class H(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())
    h = H()
    logging.getLogger().addHandler(h)
    try:
        call(load_app("privacy"), "POST", f"{DOC}/redact", {"docId": doc}, {})
    finally:
        logging.getLogger().removeHandler(h)
    assert records and not any("John Smith" in m or "john.smith@" in m for m in records)


# ---------------------------------------------------------------- Feature 10: export
def _export(env, doc, fmt, **q):
    code, r = call(load_app("export"), "GET", f"{DOC}/export", {"docId": doc}, query={"format": fmt, **q})
    assert code == 200, r
    return r, env.s3.objects[("bucket", r["objectKey"])]


def test_export_json_contains_everything(env, doc):
    # correct a field first: export must show both the AI value and the correction
    api = load_app("api")
    call(api, "POST", f"{DOC}/fields/{{fieldId}}/verify", {"docId": doc, "fieldId": "f002"},
         {"action": "correct", "correctedValue": "48 USD"})
    r, obj = _export(env, doc, "json")
    assert r["objectKey"].startswith(f"exports/{doc}/") and r["downloadUrl"].startswith("https://")
    assert obj["ContentType"] == "application/json" and obj["ServerSideEncryption"] == "AES256"
    m = json.loads(obj["Body"])
    assert set(m) >= {"document", "classification", "aiInsights", "entities", "keyDates", "actions",
                      "confidence", "fields", "tables", "extractedText"}
    total = next(f for f in m["fields"] if f["fieldId"] == "f002")
    assert total["aiValue"] == "40 USD" and total["correctedValue"] == "48 USD" and total["confidence"] == 62.0
    assert m["keyDates"][0]["isoDate"] == "2026-10-20" and "Total amount due" in m["extractedText"]
    assert env.audit_events("EXPORT_GENERATED")[0]["details"]["format"] == "json"


def test_export_csv_is_safe_and_complete(env, doc):
    env.docs.items[(doc,)]["summary"] = "=HYPERLINK(\"http://evil\")"
    r, obj = _export(env, doc, "csv")
    rows = list(csv.reader(io.StringIO(obj["Body"].decode("utf-8-sig"))))
    assert rows[0] == ["section", "item", "key", "value", "confidence", "status", "page", "notes"]
    sections = {row[0] for row in rows[1:]}
    assert {"metadata", "classification", "summary", "intent", "entity", "keyDate", "action", "field",
            "table", "text"} <= sections
    summary = next(row for row in rows if row[0] == "summary")
    assert summary[3].startswith("'=")                                           # formula injection neutralised
    field = next(row for row in rows if row[0] == "field" and row[2] == "Total")
    assert float(field[4]) == 62.0 and field[5] == "REQUIRES_REVIEW"


def test_export_pdf_is_valid(env, doc):
    r, obj = _export(env, doc, "pdf", includeText="false")
    pdf = obj["Body"]
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    # xref offsets must point at the objects they claim to
    xref_pos = int(re.search(rb"startxref\n(\d+)", pdf).group(1))
    assert pdf[xref_pos:xref_pos + 4] == b"xref"
    offsets = [int(x) for x in re.findall(rb"(\d{10}) 00000 n", pdf)]
    for n, off in enumerate(offsets, start=1):
        assert pdf[off:].startswith(f"{n} 0 obj".encode())
    text = b"".join(zlib.decompress(m) for m in re.findall(rb"stream\n(.*?)\nendstream", pdf, re.S)).decode("cp1252")
    for needle in ("Document Intelligence Report", "Key dates", "Total", "62.0%", "REQUIRES_REVIEW", "Submit report"):
        assert needle in text, needle
    assert "Extracted text" not in text


def test_export_bad_format_and_not_ready(env, doc):
    assert call(load_app("export"), "GET", f"{DOC}/export", {"docId": doc}, query={"format": "xml"})[0] == 400
    env.docs.items[(doc,)]["status"] = "PROCESSING"
    assert call(load_app("export"), "GET", f"{DOC}/export", {"docId": doc}, query={"format": "json"})[0] == 409
