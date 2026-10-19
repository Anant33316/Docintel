"""Unit tests for the shared layer (pure logic, no AWS)."""
import json
from decimal import Decimal

import pytest
from docintel_common import audit, chunking, diffing, grounding, pii, retrieval, review
from docintel_common.ddb import from_ddb, to_ddb


# ---------------------------------------------------------------- chunking
def test_chunks_respect_pages_and_size():
    pages = [{"page": 1, "lines": [{"text": "a" * 500, "confidence": 99.0}] * 5},
             {"page": 2, "lines": [{"text": "short line", "confidence": 80.0}]}]
    chunks = chunking.build_chunks(pages, max_chars=1200)
    assert {c["page"] for c in chunks} == {1, 2}
    assert all(len(c["text"]) <= 1200 for c in chunks if c["page"] == 1)
    assert chunks[-1] == {"chunkId": f"c{len(chunks):04d}", "page": 2, "text": "short line", "ocrConfidence": 80.0}


def test_line_offsets_map_back_to_pages():
    pages = [{"page": 1, "lines": [{"text": "abc"}, {"text": "de"}]}, {"page": 2, "lines": [{"text": "xyz"}]}]
    joined = "\n".join(ln["text"] for p in pages for ln in p["lines"])
    offs = chunking.line_offsets(pages)
    assert chunking.page_for_offset(offs, joined.index("de")) == 1
    assert chunking.page_for_offset(offs, joined.index("xyz")) == 2


# ---------------------------------------------------------------- retrieval
def test_bm25_ranks_relevant_chunk_first_and_reports_terms():
    items = [{"text": "The cafeteria menu for Monday"},
             {"text": "Deploy the function with AWS Lambda and Amazon SQS"},
             {"text": "Lambda calculus is a formal system"}]
    ranked = retrieval.top_k("AWS Lambda", items, k=3)
    assert ranked[0][0] is items[1]
    assert ranked[0][1]["phrase"] is True
    assert "lambda" in ranked[0][1]["matchedTerms"]
    assert all(r[0] is not items[0] for r in ranked)          # no lexical/semantic match -> excluded


def test_semantic_scores_blend_when_vectors_present():
    items = [{"text": "alpha", "vector": [1.0, 0.0]}, {"text": "beta", "vector": [0.0, 1.0]}]
    scores = retrieval.rank("unrelated words", items, query_vec=[0.0, 1.0])
    assert scores[1]["semantic"] == 1.0 and scores[1]["isMatch"]
    assert not scores[0]["isMatch"]


def test_snippet_centres_on_term():
    text = "x " * 300 + "the invoice total is 40 dollars " + "y " * 300
    s = retrieval.snippet(text, ["invoice"], width=80)
    assert "invoice" in s and s.startswith("…") and s.endswith("…")


# ---------------------------------------------------------------- grounding
SOURCES = [{"id": "S1", "docId": "d", "fileName": "f.pdf", "chunkId": "c0001", "kind": "text", "page": 2,
            "text": "Total amount due: 40 USD\nDue in 30 days", "relevance": 1.0, "lexicalScore": 1.0,
            "semanticScore": None, "ocrConfidence": 98.5, "matchedTerms": ["total"]}]


def test_grounded_answer_with_verified_quote():
    raw = json.dumps({"answer": "40 USD [S1]", "answerable": True, "citations": ["S1"],
                      "quotes": [{"source": "S1", "quote": "Total amount due: 40 USD"}], "confidence": "high"})
    r = grounding.interpret(raw, SOURCES)
    assert r["confidence"] == "high" and r["answerable"]
    assert r["citations"][0]["page"] == 2 and r["citations"][0]["snippet"] == "Total amount due: 40 USD"
    assert r["quotes"][0]["verified"] is True and not r["warnings"]


def test_fabricated_quote_and_unknown_citation_lower_confidence():
    raw = json.dumps({"answer": "100 USD [S1] [S9]", "answerable": True, "citations": ["S1", "S9"],
                      "quotes": [{"source": "S1", "quote": "Total amount due: 100 USD"}], "confidence": "high"})
    r = grounding.interpret(raw, SOURCES)
    assert r["confidence"] == "low"
    assert any("unknown sources" in w for w in r["warnings"])
    assert any("not found verbatim" in w for w in r["warnings"])


def test_unanswerable_is_explicit():
    raw = json.dumps({"answer": "I can't tell.", "answerable": False, "citations": ["S1"], "confidence": "high"})
    r = grounding.interpret(raw, SOURCES)
    assert r["answer"].startswith(grounding.NOT_ENOUGH_INFO)
    assert r["confidence"] == "none" and r["citations"] == []


def test_non_json_reply_is_unverified():
    r = grounding.interpret("The total is 40.", SOURCES)
    assert r["confidence"] == "low" and r["citations"] == [] and r["warnings"]


def test_no_sources_means_not_answerable():
    r = grounding.interpret(json.dumps({"answer": "x", "answerable": True}), [])
    assert not r["answerable"] and r["answer"].startswith(grounding.NOT_ENOUGH_INFO)


# ---------------------------------------------------------------- diffing
def test_diff_lines_and_fields():
    a = {"text": "Vendor: Acme\nTotal: 40\nDue in 30 days", "keyValues": {"Total": "40", "PO": "7"}}
    b = {"text": "Vendor: Acme\nTotal: 55\nDue in 30 days\nLate fee: 5", "keyValues": {"Total": "55", "Fee": "5"}}
    d = diffing.compare(a, b)
    assert d["text"]["added"] == ["Late fee: 5"]
    assert d["text"]["changed"][0]["before"] == "Total: 40" and d["text"]["changed"][0]["after"] == "Total: 55"
    assert d["text"]["unchangedLines"] == 2
    assert d["fields"]["changed"] == [{"key": "Total", "before": "40", "after": "55"}]
    assert d["fields"]["added"] == [{"key": "Fee", "value": "5"}]
    assert d["fields"]["removed"] == [{"key": "PO", "value": "7"}]


# ---------------------------------------------------------------- pii
def test_redact_segments_by_type_and_score():
    segs = ["Name: John Smith", "Email john@x.com", "nothing here"]
    joined, _ = pii.join_segments(segs)
    ents = [{"type": "NAME", "score": 0.99, "beginOffset": joined.index("John"), "endOffset": joined.index("John") + 10},
            {"type": "EMAIL", "score": 0.4, "beginOffset": joined.index("john@"), "endOffset": joined.index("john@") + 10}]
    out, counts = pii.redact_segments(segs, ents, min_score=0.5)
    assert out == ["Name: [NAME]", "Email john@x.com", "nothing here"]
    assert counts == {"NAME": 1}
    out, _ = pii.redact_segments(segs, ents, min_score=0.0, mask="CHAR")
    assert out[1] == "Email **********"


def test_overlapping_entities_do_not_corrupt_text():
    text = "call 555-123-4567 now"
    ents = [{"type": "PHONE", "score": 0.9, "beginOffset": 5, "endOffset": 17},
            {"type": "BANK_ACCOUNT_NUMBER", "score": 0.9, "beginOffset": 9, "endOffset": 17}]
    red, counts = pii.redact_text(text, ents)
    assert red == "call [PHONE] now" and counts == {"PHONE": 1}


def test_split_for_detection_respects_byte_limit():
    text = ("é" * 50 + "\n") * 3000
    pieces = list(pii.split_for_detection(text, max_bytes=10_000))
    assert "".join(p for _, p in pieces) == text
    assert all(len(p.encode()) <= 10_000 for _, p in pieces)
    assert pieces[1][0] == len(pieces[0][1])


def test_mask_preview_hides_value():
    assert pii.mask_preview("John Smith") == "J*** S****"


# ---------------------------------------------------------------- review
def test_review_flags_low_confidence_and_keeps_original():
    item = {"fields": [{"fieldId": "f001", "key": "Total", "value": "40", "confidence": 72.5, "page": 1},
                       {"fieldId": "f002", "key": "Date", "value": "2026-01-01", "confidence": 99.1, "page": 1}],
            "verifications": {"f001": {"status": "CORRECTED", "correctedValue": "48", "version": 1}}}
    views = {f["fieldId"]: f for f in review.fields_view(item, 90)}
    assert views["f001"]["aiValue"] == "40" and views["f001"]["effectiveValue"] == "48"
    assert views["f001"]["aiStatus"] == review.REQUIRES_REVIEW and not views["f001"]["requiresVerification"]
    assert views["f002"]["status"] == review.AUTO_ACCEPTED
    s = review.summarize(list(views.values()), 90)
    assert s["lowConfidence"] == 1 and s["pendingReview"] == 0 and s["reviewStatus"] == "REVIEWED"
    # a stricter threshold flags the second field at read time
    assert review.fields_view(item, 99.5)[1]["requiresVerification"] is True


def test_threshold_validation(monkeypatch):
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "85")
    assert review.threshold() == 85.0
    assert review.threshold("70") == 70.0
    with pytest.raises(ValueError):
        review.threshold("150")


# ---------------------------------------------------------------- audit sanitising + ddb conversion
def test_audit_sanitize_drops_content():
    clean = audit._sanitize({"question": "secret?", "answer": "x", "text": "doc", "fieldId": "f001",
                             "count": 3, "types": ["NAME", "EMAIL"], "nested": {"a": 1, "b": "drop"},
                             "longValue": "z" * 500})
    assert set(clean) == {"fieldId", "count", "types", "nested", "longValue"}
    assert clean["nested"] == {"a": 1} and len(clean["longValue"]) == audit.MAX_STR


def test_audit_rejects_unknown_event():
    with pytest.raises(ValueError):
        audit.record("SOMETHING_ELSE")


def test_ddb_round_trip():
    d = to_ddb({"a": 1.23456789, "b": [0.5, {"c": 2}], "d": True})
    assert d["a"] == Decimal("1.2346") and isinstance(d["b"][0], Decimal)
    assert from_ddb(d) == {"a": 1.2346, "b": [0.5, {"c": 2}], "d": True}
