"""API integration tests: documents are processed by the real processor code, then every route
is exercised through the Lambda handler with API Gateway proxy events (AWS replaced by fakes)."""
import json

import pytest
from helpers import INVOICE_A, INVOICE_B, call, ingest, load_app

DOC = "/documents/{docId}"


@pytest.fixture
def docs(env):
    p = load_app("processor")
    a = ingest(env, p, "uploads/aaa/invoice-a.png", INVOICE_A)
    b = ingest(env, p, "uploads/bbb/invoice-b.pdf", INVOICE_B)
    return a, b


def api():
    return load_app("api")


# ---------------------------------------------------------------- original routes stay compatible
def test_original_routes_still_work(env, docs):
    a, _ = docs
    code, body = call(api(), "POST", "/documents/upload-url", body={"fileName": "my file.pdf"})
    assert code == 200 and body["key"].startswith("uploads/") and body["key"].endswith("my_file.pdf")
    assert body["contentType"] == "application/pdf" and "uploadUrl" in body
    assert call(api(), "POST", "/documents/upload-url", body={"fileName": "x.exe"})[0] == 400

    code, body = call(api(), "GET", "/documents")
    assert code == 200 and {d["docId"] for d in body["documents"]} == set(docs)
    assert {"docId", "fileName", "status", "category", "createdAt"} <= set(body["documents"][0])

    code, body = call(api(), "GET", DOC, {"docId": a})
    assert code == 200 and body["summary"] and "resultKey" not in body and "bucket" not in body
    assert call(api(), "GET", DOC, {"docId": "nope"})[0] == 404
    assert call(api(), "GET", "/unknown")[0] == 404


def test_invalid_json_body(env, docs):
    r = api().handler({"httpMethod": "POST", "resource": "/documents/{docId}/ask",
                       "pathParameters": {"docId": docs[0]}, "body": "{nope"}, None)
    assert r["statusCode"] == 400


# ---------------------------------------------------------------- Feature 9: grounded /ask (backward compatible)
def test_ask_is_source_grounded(env, docs):
    a, _ = docs
    code, body = call(api(), "POST", f"{DOC}/ask", {"docId": a}, {"question": "What is the total amount due?"})
    assert code == 200
    assert body["question"] == "What is the total amount due?" and "40 USD" in body["answer"]   # old contract
    cite = body["citations"][0]
    assert cite["page"] == 1 and cite["fileName"] == "invoice-a.png" and "Total amount due" in cite["snippet"]
    assert body["answerable"] is True and body["confidence"] == "high"
    assert body["retrieval"]["sources"] and body["retrieval"]["mode"] == "hybrid"
    # the model only ever saw retrieved passages, never the whole document blindly
    prompt = env.bedrock.calls[-1]["messages"][-1]["content"][0]["text"]
    assert '<source id="S1"' in prompt
    ev = env.audit_events("DOCUMENT_QUERIED")[-1]
    assert "question" not in ev["details"] and "40 USD" not in json.dumps(ev["details"])


def test_ask_unanswerable_says_so(env, docs):
    code, body = call(api(), "POST", f"{DOC}/ask", {"docId": docs[0]}, {"question": "Who won the football match?"})
    assert code == 200 and body["answerable"] is False
    assert body["answer"].startswith("The document does not contain enough information")
    assert body["citations"] == [] and body["confidence"] == "none"


def test_ask_validation_and_status(env, docs):
    assert call(api(), "POST", f"{DOC}/ask", {"docId": docs[0]}, {"question": ""})[0] == 400
    env.docs.items[(docs[0],)]["status"] = "PROCESSING"
    assert call(api(), "POST", f"{DOC}/ask", {"docId": docs[0]}, {"question": "total?"})[0] == 409


def test_ask_works_for_documents_processed_before_upgrade(env, docs):
    """Legacy items: no chunks, no fields, results JSON without pageLines."""
    a, _ = docs
    for k in [k for k in env.chunks.items if k[0] == a]:
        del env.chunks.items[k]
    item = env.docs.items[(a,)]
    for attr in ("fields", "verifications", "reviewStatus", "keyDates"):
        item.pop(attr, None)
    res = json.loads(env.s3.objects[("bucket", item["resultKey"])]["Body"])
    env.s3.objects[("bucket", item["resultKey"])]["Body"] = json.dumps(
        {"text": res["text"], "keyValues": res["keyValues"], "tables": res["tables"]}).encode()
    code, body = call(api(), "POST", f"{DOC}/ask", {"docId": a}, {"question": "What is the total amount due?"})
    assert code == 200 and "40 USD" in body["answer"] and body["citations"][0]["page"] is None
    code, body = call(api(), "GET", f"{DOC}/fields", {"docId": a})
    assert code == 200 and body["fields"][0]["fieldId"] == "legacy000" and body["fields"][0]["confidence"] is None
    # legacy documents can still be verified (verifications map is created on first write)
    code, body = call(api(), "POST", f"{DOC}/fields/{{fieldId}}/verify", {"docId": a, "fieldId": "legacy001"},
                      {"action": "correct", "correctedValue": "41 USD"})
    assert code == 200 and body["field"]["effectiveValue"] == "41 USD"


# ---------------------------------------------------------------- Feature 2: chat history
def test_chat_session_lifecycle(env, docs):
    a, _ = docs
    h = {"X-User-Id": "alice"}
    code, first = call(api(), "POST", f"{DOC}/chat", {"docId": a}, {"question": "What is the total amount due?"}, headers=h)
    assert code == 200 and first["sessionId"] and "40 USD" in first["answer"]
    sid = first["sessionId"]

    code, second = call(api(), "POST", f"{DOC}/chat", {"docId": a},
                        {"question": "Where do the workloads run?", "sessionId": sid}, headers=h)
    assert code == 200 and "AWS Lambda" in second["answer"] and second["citations"][0]["page"] == 2
    # history was sent to the model as alternating turns
    msgs = env.bedrock.calls[-1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"][0]["text"] == "What is the total amount due?"

    code, hist = call(api(), "GET", f"{DOC}/chat/{{sessionId}}", {"docId": a, "sessionId": sid}, headers=h)
    assert code == 200 and [m["role"] for m in hist["messages"]] == ["user", "assistant", "user", "assistant"]
    assert hist["messages"][1]["citations"][0]["page"] == 1

    code, sessions = call(api(), "GET", f"{DOC}/chat", {"docId": a}, headers=h)
    assert sessions["sessions"][0]["sessionId"] == sid and sessions["sessions"][0]["messageCount"] == 4

    # another user cannot read or continue alice's session
    bob = {"X-User-Id": "bob"}
    assert call(api(), "GET", f"{DOC}/chat/{{sessionId}}", {"docId": a, "sessionId": sid}, headers=bob)[0] == 403
    assert call(api(), "POST", f"{DOC}/chat", {"docId": a}, {"question": "total?", "sessionId": sid}, headers=bob)[0] == 403
    assert call(api(), "GET", f"{DOC}/chat", {"docId": a}, headers=bob)[1]["sessions"] == []

    code, deleted = call(api(), "DELETE", f"{DOC}/chat/{{sessionId}}", {"docId": a, "sessionId": sid}, headers=h)
    assert code == 200 and deleted["messagesDeleted"] == 4
    assert call(api(), "GET", f"{DOC}/chat/{{sessionId}}", {"docId": a, "sessionId": sid}, headers=h)[0] == 404
    # chat items carry a TTL
    assert all("expiresAt" in i for i in env.app.items.values() if i["pk"].startswith("CHAT#")) is True


def test_chat_unknown_session(env, docs):
    code, _ = call(api(), "POST", f"{DOC}/chat", {"docId": docs[0]}, {"question": "x", "sessionId": "abc123"})
    assert code == 404


# ---------------------------------------------------------------- Feature 3 + 7: confidence + verification
def test_fields_and_verification_workflow(env, docs):
    a, _ = docs
    code, body = call(api(), "GET", f"{DOC}/fields", {"docId": a})
    assert code == 200 and body["summary"]["threshold"] == 90.0
    total = next(f for f in body["fields"] if f["key"] == "Total")
    assert total["status"] == "REQUIRES_REVIEW" and total["requiresVerification"] and total["confidence"] == 62.0
    code, only = call(api(), "GET", f"{DOC}/fields", {"docId": a}, query={"status": "REQUIRES_REVIEW"})
    assert [f["key"] for f in only["fields"]] == ["Total"]
    code, strict = call(api(), "GET", f"{DOC}/fields", {"docId": a}, query={"threshold": "99.5"})
    assert strict["summary"]["lowConfidence"] == 3
    assert call(api(), "GET", f"{DOC}/fields", {"docId": a}, query={"threshold": "abc"})[0] == 400

    fid = total["fieldId"]
    path = f"{DOC}/fields/{{fieldId}}/verify"
    assert call(api(), "POST", path, {"docId": a, "fieldId": fid}, {"action": "correct"})[0] == 400
    assert call(api(), "POST", path, {"docId": a, "fieldId": "f999"}, {"action": "confirm"})[0] == 404

    code, r = call(api(), "POST", path, {"docId": a, "fieldId": fid},
                   {"action": "correct", "correctedValue": "48 USD", "note": "checked scan"}, headers={"X-User-Id": "rev1"})
    assert code == 200
    assert r["field"]["aiValue"] == "40 USD" and r["field"]["correctedValue"] == "48 USD"
    assert r["field"]["effectiveValue"] == "48 USD" and r["field"]["status"] == "CORRECTED"
    assert r["reviewSummary"]["reviewStatus"] == "REVIEWED"

    item = env.docs.items[(a,)]
    assert next(f for f in item["fields"] if f["fieldId"] == fid)["value"] == "40 USD"   # original untouched
    assert item["verifications"][fid]["correctedValue"] == "48 USD" and item["reviewStatus"] == "REVIEWED"

    # stale optimistic-lock version is rejected
    assert call(api(), "POST", path, {"docId": a, "fieldId": fid},
                {"action": "confirm", "expectedVersion": 0})[0] == 409
    code, r2 = call(api(), "POST", path, {"docId": a, "fieldId": fid},
                    {"action": "correct", "correctedValue": "40 USD", "expectedVersion": 1}, headers={"X-User-Id": "rev2"})
    assert r2["field"]["status"] == "VERIFIED" and r2["field"]["effectiveValue"] == "40 USD"

    code, trail = call(api(), "GET", f"{DOC}/verifications", {"docId": a})
    t = trail["auditTrail"]
    assert [x["status"] for x in t] == ["CORRECTED", "VERIFIED"]
    assert t[0]["originalValue"] == "40 USD" and t[0]["correctedValue"] == "48 USD" and t[0]["verifiedBy"] == "rev1"
    assert t[1]["previousCorrectedValue"] == "48 USD" and t[1]["verifiedAt"]
    assert len(env.audit_events("FIELD_VERIFIED")) == 2
    assert len(env.audit_events("RESULT_MODIFIED")) == 2         # correction, then reverting a correction
    assert "48 USD" not in json.dumps([e["details"] for e in env.audit_events()])


# ---------------------------------------------------------------- Feature 1: comparison
def test_compare_separates_facts_from_ai(env, docs):
    a, b = docs
    code, r = call(api(), "POST", "/compare", body={"docIdA": a, "docIdB": b})
    assert code == 200 and r["cached"] is False
    fact = r["factualDiff"]
    assert fact["method"] == "deterministic"
    assert "Late fee: 5 USD applies after the due date" in fact["text"]["added"]
    assert {"key": "Total", "before": "40 USD", "after": "55 USD"} in fact["fields"]["changed"]
    ai = r["aiInterpretation"]
    assert ai["generatedBy"] == "LLM" and ai["status"] == "OK" and "disclaimer" in ai
    assert ai["added"][0]["verified"] is True
    assert ai["contradictions"][0]["verified"] is False         # evidence not in either document
    assert r["summary"]

    calls = len(env.bedrock.calls)
    code, again = call(api(), "POST", "/compare", body={"docIdA": a, "docIdB": b})
    assert again["cached"] is True and len(env.bedrock.calls) == calls
    assert call(api(), "GET", "/comparisons/{comparisonId}", {"comparisonId": r["comparisonId"]})[1]["docIdB"] == b
    listing = call(api(), "GET", f"{DOC}/comparisons", {"docId": b})[1]
    assert listing["comparisons"][0]["comparisonId"] == r["comparisonId"]
    assert len(env.audit_events("DOCUMENT_COMPARED")) == 2
    assert call(api(), "POST", "/compare", body={"docIdA": a, "docIdB": a})[0] == 400


def test_compare_survives_bedrock_failure(env, docs):
    def boom(system, messages):
        raise RuntimeError("throttled")
    env.bedrock.responder = boom
    code, r = call(api(), "POST", "/compare", body={"docIdA": docs[0], "docIdB": docs[1], "force": True})
    assert code == 200 and r["aiInterpretation"]["status"] == "FAILED" and r["factualDiff"]["text"]["added"]


# ---------------------------------------------------------------- Feature 5: search
def test_search_ranks_and_filters(env, docs):
    a, b = docs
    code, r = call(api(), "GET", "/search", query={"q": "AWS Lambda"})
    assert code == 200 and r["results"][0]["docId"] == a and r["searchMode"] == "hybrid"
    snip = r["results"][0]["snippets"][0]
    assert "Lambda" in snip["text"] and snip["page"] == 2

    code, r = call(api(), "GET", "/search", query={"q": "late fee"})
    assert r["results"][0]["docId"] == b

    code, r = call(api(), "GET", "/search", query={"q": "invoice", "status": "FAILED"})
    assert r["results"] == []
    env.docs.items[(a,)]["createdAt"] = 1_700_000_000          # 2023-11-14
    code, r = call(api(), "GET", "/search", query={"q": "invoice", "dateTo": "2024-01-01"})
    assert [x["docId"] for x in r["results"]] == [a]
    code, r = call(api(), "GET", "/search", query={"category": "invoice"})
    assert r["searchMode"] == "browse" and r["total"] == 2
    assert call(api(), "GET", "/search", query={"q": "x", "dateFrom": "01-01-2024"})[0] == 400
    assert call(api(), "GET", "/search", query={"q": "x", "status": "BOGUS"})[0] == 400
    ev = env.audit_events("DOCUMENTS_SEARCHED")[0]
    assert "q" not in ev["details"] and "query" not in ev["details"]


def test_search_lexical_mode_without_embeddings(env, docs, monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "")
    for k in list(env.chunks.items):
        env.chunks.items[k].pop("vector", None)
    code, r = call(api(), "GET", "/search", query={"q": "Lambda"})
    assert r["searchMode"] == "lexical" and r["results"][0]["docId"] == docs[0]


# ---------------------------------------------------------------- Feature 6 + 11: stats and audit
def test_stats_dashboard(env, docs):
    a, _ = docs
    call(api(), "POST", f"{DOC}/ask", {"docId": a}, {"question": "What is the total amount due?"})
    env.docs.put_item(Item={"docId": "failed1", "fileName": "bad.pdf", "status": "FAILED", "createdAt": 1,
                            "errorMessage": "no text found in document"})
    env.docs.put_item(Item={"docId": "proc1", "fileName": "busy.pdf", "status": "PROCESSING", "createdAt": 2})
    code, s = call(api(), "GET", "/stats")
    assert code == 200
    assert s["documents"] == {"total": 4, "completed": 2, "failed": 1, "processing": 1,
                              "byStatus": {"COMPLETED": 2, "FAILED": 1, "PROCESSING": 1}}
    assert s["categories"] == {"invoice": 2}
    assert s["processingTimeSeconds"]["samples"] == 2 and s["processingTimeSeconds"]["average"] is not None
    assert s["failures"]["recent"][0]["error"] == "no text found in document"
    assert len(s["recentUploads"]) == 4 and len(s["uploadsPerDay"]) == 14
    assert s["ai"]["questionsAnswered"] == 1 and s["ai"]["textractPages"] == 3
    assert s["ai"]["bedrockInputTokens"] > 0
    assert s["review"]["documentsPendingReview"] == 1
    assert isinstance(s["upcomingKeyDates"], list)   # contents depend on today's date


def test_audit_endpoints(env, docs):
    a, _ = docs
    code, r = call(api(), "GET", f"{DOC}/audit", {"docId": a})
    types = [e["eventType"] for e in r["events"]]
    assert code == 200 and "DOCUMENT_PROCESSED" in types and "DOCUMENT_UPLOADED" in types
    day = r["events"][0]["timestamp"][:10]
    code, r = call(api(), "GET", "/audit", query={"date": day, "eventType": "DOCUMENT_PROCESSED"})
    assert code == 200 and len(r["events"]) == 2
    assert call(api(), "GET", "/audit", query={"date": "yesterday"})[0] == 400


def test_same_millisecond_writes_do_not_collide(env, docs, monkeypatch):
    import qa
    import review_api
    monkeypatch.setattr(qa, "now_ms", lambda: 1_800_000_000_000)
    monkeypatch.setattr(review_api, "now_iso", lambda: "2027-01-15T00:00:00.000Z")
    a, _ = docs
    _, first = call(api(), "POST", f"{DOC}/chat", {"docId": a}, {"question": "What is the total amount due?"})
    call(api(), "POST", f"{DOC}/chat", {"docId": a}, {"question": "Where do workloads run?", "sessionId": first["sessionId"]})
    _, hist = call(api(), "GET", f"{DOC}/chat/{{sessionId}}", {"docId": a, "sessionId": first["sessionId"]})
    assert len(hist["messages"]) == 4
    path = f"{DOC}/fields/{{fieldId}}/verify"
    call(api(), "POST", path, {"docId": a, "fieldId": "f002"}, {"action": "confirm"})
    call(api(), "POST", path, {"docId": a, "fieldId": "f002"}, {"action": "correct", "correctedValue": "41 USD"})
    assert len(call(api(), "GET", f"{DOC}/verifications", {"docId": a})[1]["auditTrail"]) == 2
