"""Explainable anomaly rules (unit) and the invoice / anomaly API (integration with in-memory fakes)."""
import json

import pytest
from docintel_common import anomalies, invoice
from helpers import call, ingest, load_app

GOOD = ["Globex Supplies Ltd - Invoice", "Invoice No: GX-100", "Invoice Date: 2026-04-03", "Due Date: 2026-05-03",
        "Subtotal: $1,250.00", "Tax (18%): $225.00", "Grand Total: $1,475.00", "Terms: Net 30"]


def item(doc_id, lines, category="invoice", **extra):
    pl = [{"page": 1, "lines": [{"text": t} for t in lines]}]
    return {"docId": doc_id, "fileName": f"{doc_id}.pdf", "status": "COMPLETED", "category": category,
            "invoice": invoice.extract(pl), **extra}


def rules(findings, doc_id=None):
    return sorted(f["rule"] for f in findings if doc_id is None or f["docId"] == doc_id)


def edit(lines, old, new):
    return [ln.replace(old, new) for ln in lines]


# ---------------------------------------------------------------- unit: no false positives
def test_consistent_invoice_has_no_findings():
    assert anomalies.detect([item("a", GOOD)]) == []


def test_non_invoices_are_not_checked():
    contract = ["Services Agreement", "Total contract value: 50,000 USD", "Effective date: 2026-01-01"]
    assert anomalies.detect([item("c", contract, category="contract")]) == []
    assert anomalies.detect([item("p", GOOD, status="FAILED")]) == []


# ---------------------------------------------------------------- unit: each rule
def test_missing_required_fields_have_explicit_severity():
    f = anomalies.detect([item("a", ["INVOICE", "Invoice No: 7", "Subtotal: 10.00"])])
    sev = {x["evidence"][0]["field"]: x["severity"] for x in f if x["rule"] == "MISSING_FIELD"}
    assert sev == {"total": "medium", "invoiceDate": "low", "vendorName": "low"}


def test_arithmetic_severity_depends_on_size_of_difference():
    big = anomalies.detect([item("a", edit(GOOD, "1,475.00", "1,575.00"))])
    m = next(x for x in big if x["rule"] == "ARITHMETIC_MISMATCH")
    assert m["severity"] == "high" and "100.00" in m["explanation"]
    assert {e["field"] for e in m["evidence"]} == {"subtotal", "tax", "total"}
    assert all(e["snippet"] for e in m["evidence"]) and "fraud" not in json.dumps(big).lower()
    small = anomalies.detect([item("a", edit(GOOD, "1,475.00", "1,475.50"))])
    assert next(x for x in small if x["rule"] == "ARITHMETIC_MISMATCH")["severity"] == "low"


def test_other_document_rules():
    f = anomalies.detect([item("a", ["Acme - Invoice", "Invoice No: 9", "Invoice Date: 2026-03-10",
                                     "Due Date: 2026-03-01", "Subtotal: 100.00", "VAT (20%): 25.00",
                                     "Total: 125.00"])])
    assert rules(f) == ["DUE_BEFORE_ISSUE", "TAX_RATE_MISMATCH"]
    f = anomalies.detect([item("b", ["Acme - Invoice", "Invoice No: 9", "Invoice Date: 2026-03-10",
                                     "Subtotal: 100.00", "Total: 112.00"])])
    assert rules(f) == ["UNEXPLAINED_DIFFERENCE"]
    f = anomalies.detect([item("c", ["Acme - Invoice", "Invoice No: 9", "Invoice Date: 2026-03-10",
                                     "Total: (40.00)"])])
    assert rules(f) == ["NEGATIVE_AMOUNT"]
    f = anomalies.detect([item("d", ["Acme - Invoice", "Invoice No: 9", "Invoice Date: 04/05/2026", "Total: 5"])])
    assert rules(f) == ["AMBIGUOUS_FIELD"]
    f = anomalies.detect([item("e", ["Acme - Invoice", "Invoice No: 9", "Invoice Date: 31/02/2026", "Total: 5"])])
    assert rules(f) == ["INVALID_VALUE"]


def test_high_value_threshold_is_configurable(monkeypatch):
    assert rules(anomalies.detect([item("a", GOOD)])) == []
    monkeypatch.setenv("HIGH_VALUE_THRESHOLD", "1000")
    f = anomalies.detect([item("a", GOOD)])
    assert rules(f) == ["HIGH_VALUE"] and f[0]["severity"] == "info"


# ---------------------------------------------------------------- unit: cross-document rules
def test_duplicate_number_and_duplicate_submission():
    other = edit(GOOD, "2026-04-03", "2026-04-20")
    f = anomalies.detect([item("a", GOOD), item("b", other)])
    assert rules(f, "a") == ["DUPLICATE_INVOICE_NUMBER"] and rules(f, "b") == ["DUPLICATE_INVOICE_NUMBER"]
    assert f[0]["relatedDocs"][0]["docId"] in ("a", "b") and f[0]["severity"] == "medium"
    f = anomalies.detect([item("a", GOOD), item("b", GOOD)])
    assert rules(f) == ["POSSIBLE_DUPLICATE_SUBMISSION"] * 2 and f[0]["severity"] == "high"


def test_different_vendors_may_share_an_invoice_number():
    other = edit(GOOD, "Globex Supplies Ltd", "Initech LLC")
    assert anomalies.detect([item("a", GOOD), item("b", other)]) == []


def test_versions_are_not_duplicates_but_changes_are_reported():
    v2 = edit(GOOD, "1,475.00", "1,593.00")
    v2 = edit(v2, "$225.00", "$243.00")
    v2 = edit(v2, "1,250.00", "1,350.00")
    f = anomalies.detect([item("a", GOOD, versionGroup="a", versionNumber=1),
                          item("b", v2, versionGroup="a", versionNumber=2)])
    assert set(rules(f)) == {"VERSION_CHANGE"} and all(x["docId"] == "b" for x in f)
    total = next(x for x in f if x["fields"] == ["total"])
    assert "1,475.00" in total["explanation"] and "1,593.00" in total["explanation"]
    assert total["relatedDocs"] == [{"docId": "a", "fileName": "a.pdf"}]


def test_corrections_resolve_findings_and_change_ids():
    wrong = item("a", edit(GOOD, "1,475.00", "1,575.00"))
    before = anomalies.detect([wrong])
    assert anomalies.detect([wrong])[0]["findingId"] == before[0]["findingId"]          # stable ids
    wrong["verifications"] = {"inv_total": {"status": "CORRECTED", "correctedValue": "$1,475.00"}}
    assert anomalies.detect([wrong]) == []
    assert wrong["invoice"]["fields"]["total"]["value"] == "1575.00"                       # original kept


# ---------------------------------------------------------------- API integration (real handlers, fakes)
def text_doc(*lines):
    return {"pages": [[(t, 99.0) for t in lines]]}          # LINE blocks only, like text extraction


@pytest.fixture
def docs(env):
    p = load_app("processor")
    a = ingest(env, p, "uploads/a/inv-a.pdf", text_doc(*edit(GOOD, "1,475.00", "1,575.00")))
    b = ingest(env, p, "uploads/b/inv-b.pdf", text_doc(*GOOD))
    return a, b


def api():
    return load_app("api")


def test_invoice_endpoint_and_review_fields(env, docs):
    a, _ = docs
    code, r = call(api(), "GET", "/documents/{docId}/invoice", {"docId": a})
    assert code == 200 and r["available"] and r["invoiceLike"] and r["fields"]["total"]["value"] == "1575.00"
    assert next(c for c in r["checks"] if c["id"] == "arithmetic")["status"] == "fail"
    assert {f["rule"] for f in r["findings"]} >= {"ARITHMETIC_MISMATCH", "DUPLICATE_INVOICE_NUMBER"}
    item = env.docs.items[(a,)]
    assert item["reviewStatus"] == "PENDING_REVIEW"              # parser fields have no confidence -> review
    assert {f["fieldId"] for f in item["fields"]} >= {"inv_total", "inv_invoiceNumber"}
    assert call(api(), "GET", "/documents/{docId}/invoice", {"docId": "nope"})[0] == 404
    env.docs.items[(a,)]["status"] = "PROCESSING"
    assert call(api(), "GET", "/documents/{docId}/invoice", {"docId": a})[0] == 409


def test_anomaly_list_filters_and_review_workflow(env, docs):
    a, _ = docs
    code, r = call(api(), "GET", "/anomalies")
    assert code == 200 and r["documentsChecked"] == 2 and r["summary"]["open"] == len(r["findings"])
    high = call(api(), "GET", "/anomalies", query={"severity": "high"})[1]["findings"]
    assert {f["rule"] for f in high} == {"ARITHMETIC_MISMATCH"}
    assert call(api(), "GET", "/anomalies", query={"status": "weird"})[0] == 400
    fid = high[0]["findingId"]
    path = "/documents/{docId}/anomalies/{findingId}/review"
    assert call(api(), "POST", path, {"docId": a, "findingId": fid}, {"action": "dismiss"})[0] == 400   # note needed
    assert call(api(), "POST", path, {"docId": a, "findingId": "0000000000000000"}, {"action": "acknowledge"})[0] == 404
    assert call(api(), "POST", path, {"docId": a, "findingId": fid}, {"action": "delete"})[0] == 400
    code, d = call(api(), "POST", path, {"docId": a, "findingId": fid},
                   {"action": "dismiss", "note": "Late fee agreed by phone"}, headers={"X-User-Id": "ann"})
    assert code == 200 and d["finding"]["status"] == "dismissed" and d["finding"]["review"]["decidedBy"] == "ann"
    r = call(api(), "GET", "/anomalies", query={"status": "dismissed"})[1]
    assert [f["findingId"] for f in r["findings"]] == [fid] and r["summary"]["dismissed"] == 1
    ev = env.audit_events("ANOMALY_REVIEWED")[-1]
    assert ev["details"]["action"] == "dismiss" and "Late fee" not in json.dumps(ev["details"])
    code, d = call(api(), "POST", path, {"docId": a, "findingId": fid}, {"action": "reopen"})
    assert d["finding"]["status"] == "open" and len(env.audit_events("ANOMALY_REVIEWED")) == 2


def test_field_correction_resolves_the_finding(env, docs):
    a, _ = docs
    code, _ = call(api(), "POST", "/documents/{docId}/fields/{fieldId}/verify", {"docId": a, "fieldId": "inv_total"},
                   {"action": "correct", "correctedValue": "$1,475.00"})
    assert code == 200
    r = call(api(), "GET", "/documents/{docId}/invoice", {"docId": a})[1]
    assert r["fields"]["total"]["status"] == "corrected" and r["fields"]["total"]["original"]["value"] == "1575.00"
    assert "ARITHMETIC_MISMATCH" not in {f["rule"] for f in r["findings"]}
    # now both documents have identical number, total and date -> possible duplicate submission
    assert "POSSIBLE_DUPLICATE_SUBMISSION" in {f["rule"] for f in r["findings"]}


def test_ocr_key_values_feed_the_invoice_parser(env):
    from helpers import INVOICE_A
    doc_id = ingest(env, load_app("processor"), "uploads/f/fixture.pdf", INVOICE_A)
    f = env.docs.items[(doc_id,)]["invoice"]["fields"]
    assert f["invoiceNumber"]["value"] == "INV-001" and f["invoiceNumber"]["evidence"] == "Invoice Number: INV-001"
    assert f["total"]["raw"] == "40 USD" and f["total"]["evidence"] == "Total amount due: 40 USD"   # text line wins
    assert env.docs.items[(doc_id,)]["fields"][0]["fieldId"] == "f001"         # OCR fields stay the review fields


def test_json_export_includes_invoice_fields_and_checks(env, docs):
    a, _ = docs
    code, r = call(load_app("export"), "GET", "/documents/{docId}/export", {"docId": a}, query={"format": "json"})
    model = json.loads(env.s3.objects[("bucket", r["objectKey"])]["Body"])
    assert model["invoice"]["fields"]["total"]["value"] == "1575.00"
    assert any(c["id"] == "arithmetic" and c["status"] == "fail" for c in model["invoice"]["checks"])
