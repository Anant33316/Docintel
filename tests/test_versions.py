"""Document version history (integration: real API + processor handlers, in-memory fakes)."""
import json

import pytest
from helpers import call, ingest, load_app

V1 = ["Northwind Cloud Consulting - Invoice", "Invoice Number: NW-2026-001", "Invoice Date: September 15, 2026",
      "Total Amount Due: 4,800 USD", "Payment Terms: Net 30."]
V2 = ["Northwind Cloud Consulting - Invoice", "Invoice Number: NW-2026-001-R1", "Invoice Date: September 22, 2026",
      "Total Amount Due: 5,350 USD", "Payment Terms: Due on receipt.", "A late fee of 2% per month applies."]
P = "/documents/{docId}/versions"


def spec(lines):
    return {"pages": [[(t, 99.0) for t in lines]]}


@pytest.fixture
def docs(env):
    p = load_app("processor")
    return (ingest(env, p, "uploads/1/v1.pdf", spec(V1)), ingest(env, p, "uploads/2/v2.pdf", spec(V2)),
            ingest(env, p, "uploads/3/v1-copy.pdf", spec(V1)))


def api():
    return load_app("api")


def test_link_and_timeline_with_deterministic_differences(env, docs):
    v1, v2, _ = docs
    before = json.dumps(env.docs.items[(v1,)]["invoice"], sort_keys=True, default=str)
    code, r = call(api(), "POST", P, {"docId": v2}, {"previousDocId": v1}, headers={"X-User-Id": "ann"})
    assert code == 200 and r == {"docId": v2, "versionGroup": v1, "versionNumber": 2, "versionOf": v1}
    assert json.dumps(env.docs.items[(v1,)]["invoice"], sort_keys=True, default=str) == before   # original kept
    code, t = call(api(), "GET", P, {"docId": v1})
    assert code == 200 and [v["versionNumber"] for v in t["versions"]] == [1, 2]
    assert t["versions"][1]["total"] == "5,350 USD"
    ch = t["changes"][0]
    changed = {c["field"]: (c["before"], c["after"]) for c in ch["fields"]["changed"]}
    assert changed["total"] == ("4,800 USD", "5,350 USD") and changed["invoiceNumber"][1] == "NW-2026-001-R1"
    assert changed["paymentTerms"] == ("Net 30", "Due on receipt")
    assert ch["text"]["addedCount"] >= 1 and "A late fee of 2% per month applies." in ch["text"]["added"]
    assert ch["identicalText"] is False and ch["method"].startswith("deterministic")
    assert env.audit_events("VERSION_LINKED")[0]["actor"] == "ann"
    listed = {d["docId"]: d for d in call(api(), "GET", "/documents")[1]["documents"]}
    assert listed[v2]["versionNumber"] == 2 and listed[v1]["versionGroup"] == v1


def test_versions_change_anomalies_but_not_duplicates(env, docs):
    v1, v2, copy = docs
    call(api(), "POST", P, {"docId": v2}, {"previousDocId": v1})
    rules = {(f["docId"], f["rule"]) for f in call(api(), "GET", "/anomalies")[1]["findings"]}
    assert (v2, "VERSION_CHANGE") in rules
    # the unlinked identical copy of v1 is still reported as a possible duplicate of v1
    assert (copy, "POSSIBLE_DUPLICATE_SUBMISSION") in rules and (v1, "POSSIBLE_DUPLICATE_SUBMISSION") in rules
    call(api(), "POST", P, {"docId": copy}, {"previousDocId": v2})
    after = {(f["docId"], f["rule"]) for f in call(api(), "GET", "/anomalies")[1]["findings"]}
    assert not any(r == "POSSIBLE_DUPLICATE_SUBMISSION" for _, r in after)


def test_third_version_and_identical_text(env, docs):
    v1, v2, copy = docs
    call(api(), "POST", P, {"docId": v2}, {"previousDocId": v1})
    code, r = call(api(), "POST", P, {"docId": copy}, {"previousDocId": v1})        # appended at the end
    assert code == 200 and r["versionNumber"] == 3
    t = call(api(), "GET", P, {"docId": copy})[1]
    assert [v["docId"] for v in t["versions"]] == [v1, v2, copy] and len(t["changes"]) == 2


def test_invalid_links_are_rejected(env, docs):
    v1, v2, copy = docs
    assert call(api(), "POST", P, {"docId": v1}, {"previousDocId": v1})[0] == 400
    assert call(api(), "POST", P, {"docId": v1}, {})[0] == 400
    assert call(api(), "POST", P, {"docId": v1}, {"previousDocId": "missing"})[0] == 404
    assert call(api(), "POST", P, {"docId": v2}, {"previousDocId": v1})[0] == 200
    assert call(api(), "POST", P, {"docId": v2}, {"previousDocId": copy})[0] == 409            # already linked
    env.docs.items[(copy,)]["status"] = "FAILED"
    assert call(api(), "POST", P, {"docId": copy}, {"previousDocId": v1})[0] == 409            # failed extraction
    code, t = call(api(), "GET", P, {"docId": copy})
    assert code == 200 and t["versionGroup"] is None and len(t["versions"]) == 1 and t["changes"] == []
    assert call(api(), "GET", P, {"docId": "missing"})[0] == 404
