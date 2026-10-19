"""Processor tests: full S3 -> Textract -> Comprehend -> Bedrock -> DynamoDB/S3 flow against fakes."""
import json

from fakes import make_blocks
from helpers import INVOICE_A, ingest, load_app, s3_event

KEY_A = "uploads/aaa/invoice-a.png"


def test_parse_blocks_keeps_confidence_and_pages():
    p = load_app("processor")
    r = p.parse_blocks(make_blocks(INVOICE_A["pages"], INVOICE_A["kvs"], INVOICE_A["tables"]))
    total = next(f for f in r["fields"] if f["key"] == "Total")
    assert total == {"fieldId": "f002", "key": "Total", "value": "40 USD", "keyConfidence": 95.0,
                     "valueConfidence": 62.0, "confidence": 62.0, "page": 1}
    assert [pl["page"] for pl in r["pageLines"]] == [1, 2]
    assert r["pageLines"][1]["lines"][0] == {"text": "Bill to: John Smith, 12 Baker Street", "confidence": 97.0}
    assert r["keyValues"]["Total"] == "40 USD" and r["pages"] == 2
    assert r["tables"] == [[["Item", "Price"], ["Compute", "40"]]]
    assert 96 < r["avgConfidence"] < 100


def test_full_processing_flow(env):
    p = load_app("processor")
    doc_id = ingest(env, p, KEY_A, INVOICE_A)
    item = env.docs.items[(doc_id,)]

    assert item["status"] == "COMPLETED" and item["category"] == "invoice"
    # Feature 3: confidence kept, low-confidence field flagged against the threshold (90)
    fields = {f["key"]: f for f in item["fields"]}
    assert fields["Total"]["reviewStatus"] == "REQUIRES_REVIEW"
    assert fields["Invoice Number"]["reviewStatus"] == "AUTO_ACCEPTED"
    assert item["reviewStatus"] == "PENDING_REVIEW" and item["reviewSummary"]["lowConfidence"] == 1
    assert item["verifications"] == {}
    # Comprehend scores preserved
    assert all("score" in e for e in item["entities"])
    # Feature 8: key dates/actions, with the hallucinated item flagged and its invalid ISO date dropped
    deadline = next(d for d in item["keyDates"] if d["type"] == "deadline")
    assert deadline["isoDate"] == "2026-10-20" and deadline["grounded"] is True
    fake = next(d for d in item["keyDates"] if d["type"] == "renewal")
    assert fake["grounded"] is False and fake["isoDate"] is None
    assert item["actions"][0]["action"] == "Submit report" and item["actions"][0]["responsibleParty"] == "Finance team"
    # stats
    assert item["processingMs"] >= 0 and item["aiStats"]["bedrockCalls"] == 2
    assert item["aiStats"]["comprehendCalls"] == 3 and item["aiStats"]["textractPages"] == 2

    # retrieval index: page-aware text chunks + field chunks with vectors
    chunks = [c for (d, _), c in env.chunks.items.items() if d == doc_id]
    assert {c["kind"] for c in chunks} == {"text", "field"}
    assert {c["page"] for c in chunks if c["kind"] == "text"} == {1, 2}
    assert all(isinstance(c["vector"], bytes) for c in chunks)

    # results object keeps the original structure plus pageLines/fields
    res = json.loads(env.s3.objects[("bucket", f"results/{doc_id}.json")]["Body"])
    assert set(res) >= {"text", "keyValues", "tables", "pageLines", "fields"}

    # Feature 11: audit trail, without document content
    types = [e["eventType"] for e in env.audit_events()]
    assert "DOCUMENT_UPLOADED" in types and "DOCUMENT_PROCESSED" in types
    assert "John Smith" not in json.dumps([e["details"] for e in env.audit_events()])
    counters = env.app.items[("STATS", "COUNTERS")]
    assert counters["count_DOCUMENT_PROCESSED"] == 1 and counters["bedrockInputTokens"] == 200


def test_duplicate_event_is_idempotent(env):
    p = load_app("processor")
    doc_id = ingest(env, p, KEY_A, INVOICE_A)
    calls_before = len(env.bedrock.calls)
    assert p.handler(s3_event(KEY_A, "e1"), None) == {"batchItemFailures": []}
    assert len(env.bedrock.calls) == calls_before                 # skipped: already COMPLETED
    assert env.docs.items[(doc_id,)]["status"] == "COMPLETED"


def test_failure_marks_failed_and_reports_batch_item(env):
    p = load_app("processor")
    env.textract.docs[KEY_A] = make_blocks([[]])                 # no text at all
    out = p.handler(s3_event(KEY_A), None)
    assert out == {"batchItemFailures": [{"itemIdentifier": f"m-{KEY_A}"}]}
    item = next(iter(env.docs.items.values()))
    assert item["status"] == "FAILED" and "no text" in item["errorMessage"]
    failed = env.audit_events("DOCUMENT_FAILED")
    assert failed and failed[0]["details"] == {"errorType": "ValueError"}


def test_embeddings_unavailable_does_not_fail_processing(env):
    env.bedrock.embeddings = False
    p = load_app("processor")
    doc_id = ingest(env, p, KEY_A, INVOICE_A)
    assert env.docs.items[(doc_id,)]["status"] == "COMPLETED"
    assert all("vector" not in c for (d, _), c in env.chunks.items.items() if d == doc_id)


def test_reprocessing_removes_stale_chunks(env):
    p = load_app("processor")
    doc_id = ingest(env, p, KEY_A, INVOICE_A)
    env.chunks.put_item(Item={"docId": doc_id, "chunkId": "c9999", "text": "stale"})
    env.docs.items[(doc_id,)]["status"] = "FAILED"             # allow a re-claim
    p.handler(s3_event(KEY_A, "e1"), None)
    assert (doc_id, "c9999") not in env.chunks.items


def test_unsupported_extension_is_skipped(env):
    p = load_app("processor")
    assert p.handler(s3_event("uploads/x/readme.txt"), None) == {"batchItemFailures": []}
    assert not env.docs.items
