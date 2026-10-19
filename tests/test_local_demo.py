"""Local demo: PDF upload/extraction, honest local stand-ins, presigned-URL emulation, routing and security.

Server tests need FastAPI (``pip install fastapi uvicorn``); PDF tests also need PyMuPDF
(``pip install pymupdf==1.28.2``). Both are skipped, not failed, when the package is missing.
No HTTP client library is needed: requests are sent to the ASGI app in-process.
"""
import asyncio
import hashlib
import json
import os
import sys
import urllib.parse

import pytest
from docintel_common import aws
from helpers import ROOT, call, ingest, INVOICE_A, load_app

sys.path.insert(0, ROOT)
from local_demo import heuristics  # noqa: E402
from local_demo.services import LocalBedrock, LocalComprehend  # noqa: E402

HOST = "127.0.0.1:8001"


# ---------------------------------------------------------------- tiny in-process ASGI client
class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body)


def request(app, method, path, body=b"", headers=None):
    async def run():
        url = urllib.parse.urlsplit(path)
        hdrs = {"host": HOST, **{k.lower(): v for k, v in (headers or {}).items()}}
        if body:
            hdrs.setdefault("content-length", str(len(body)))
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": urllib.parse.unquote(url.path),
                 "raw_path": url.path.encode(), "query_string": url.query.encode(), "root_path": "",
                 "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
                 "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8001)}
        done, sent, out = asyncio.Event(), [False], {"status": None, "headers": {}, "body": b""}

        async def receive():
            if not sent[0]:
                sent[0] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")
                if not msg.get("more_body"):
                    done.set()

        await app(scope, receive, send)
        return Resp(out["status"], out["headers"], out["body"])

    return asyncio.run(run())


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def make_app(monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "")         # what local_server.py configures by default
    import local_server

    def build(**kw):
        return local_server.create_app(**kw)
    yield build
    aws.reset_overrides()


@pytest.fixture
def app(make_app):
    return make_app()


def _pymupdf():
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            pytest.skip("PyMuPDF not installed")
    return pymupdf


def text_pdf(pages):
    """A PDF with a real text layer, made with the project's own dependency-free writer."""
    sys.path.insert(0, os.path.join(ROOT, "src", "export"))
    from pdf_report import PdfReport
    pdf = PdfReport("test document")
    for i, lines in enumerate(pages):
        if i:
            pdf._new_page()
        for ln in lines:
            pdf.text(ln)
    return pdf.build()


def sample(name="sample_invoice_v1.pdf"):
    with open(os.path.join(ROOT, "samples", name), "rb") as f:
        return f.read()


def upload(app, data, name="doc.pdf", headers=None):
    return request(app, "POST", f"/local/upload?fileName={urllib.parse.quote(name)}", data,
                   {"content-type": "application/pdf", **(headers or {})})


# ---------------------------------------------------------------- heuristics (no extra packages)
def test_dates_are_parsed_only_when_unambiguous():
    found = {w: iso for w, iso, _ in heuristics.find_dates(
        "Due October 15, 2026; signed 3 March 2027; ref 2026-01-31; local 04/05/2026; around May 2026")}
    assert found["October 15, 2026"] == "2026-10-15" and found["3 March 2027"] == "2027-03-03"
    assert found["2026-01-31"] == "2026-01-31"
    assert found["04/05/2026"] is None and found["May 2026"] is None        # ambiguous -> no ISO date
    assert list(heuristics.find_dates("February 30, 2026"))[0][1] is None   # impossible date


def test_key_dates_quote_their_source_line():
    lines = ["Invoice Date: September 15, 2026", "Payment is due by October 15, 2026.",
             "The contract renews on January 1, 2027."]
    kd = heuristics.key_dates(lines)
    assert [(d["type"], d["isoDate"]) for d in kd] == [("other", "2026-09-15"), ("payment", "2026-10-15"),
                                                       ("renewal", "2027-01-01")]
    assert kd[0]["description"] == "Invoice Date" and all(d["sourceText"] in lines for d in kd)


def test_classification_needs_two_cues():
    assert heuristics.classify("INVOICE\nInvoice Number: 1\nAmount due: 4 USD")[0] == "invoice"
    assert heuristics.classify("A note that mentions an invoice once")[0] == "other"


def test_extractive_summary_copies_lines_verbatim():
    lines = ["Northwind - Invoice", "Hello", "Total Amount Due: 4,800 USD", "Due by October 15, 2026."]
    s = heuristics.extractive_summary(lines)
    assert all(part in lines for part in s.split("\n")) and s.startswith("Northwind")


def test_pii_patterns_do_not_pretend_to_find_names():
    text = "Bill To: Priya Sharma, 42 Market Street\nEmail: p@example.com Phone: 555-201-3344\nAccount Number: 00123456789"
    types = {e["Type"] for e in heuristics.pii_entities(text)}
    assert types == {"ADDRESS", "EMAIL", "PHONE", "BANK_ACCOUNT_NUMBER"}
    acct = next(e for e in heuristics.pii_entities(text) if e["Type"] == "BANK_ACCOUNT_NUMBER")
    assert text[acct["BeginOffset"]:acct["EndOffset"]] == "00123456789"


def test_local_bedrock_handles_every_production_prompt(env):
    """Drive the stand-in with the REAL production prompts, so a prompt change cannot go unnoticed."""
    processor = load_app("processor")
    import comparison
    import qa
    br = LocalBedrock()
    aws.override("bedrock-runtime", br)
    text = "ACME Invoice\nInvoice Number: 7\nTotal amount due: 40 USD\nPay before May 1, 2026."

    out = processor.summarize_and_classify(text)
    assert out["category"] == "invoice" and out["intent"] == "" and out["summary"].startswith("ACME Invoice")
    kd = processor.extract_key_dates(text)
    assert kd["actions"] == [] and kd["keyDates"][0]["isoDate"] == "2026-05-01" and kd["keyDates"][0]["grounded"]

    item = {"docId": "d1", "fileName": "a.pdf", "status": "COMPLETED"}
    chunks = [{"chunkId": "c0001", "kind": "text", "page": 1, "text": text}]
    orig = qa.load_chunks
    qa.load_chunks = lambda _item: chunks
    try:
        res, usage = qa.answer_question(item, "What is the total amount due?")
        assert res["answerable"] and res["quotes"][0]["verified"] and "40 USD" in res["answer"]
        assert usage == {"inputTokens": 0, "outputTokens": 0}
        res, _ = qa.answer_question(item, "Who won the football match?")
        assert res["answerable"] is False
    finally:
        qa.load_chunks = orig
    with pytest.raises(RuntimeError):
        comparison._semantic("a", "b", {"text": {"addedCount": 0, "removedCount": 0, "changedCount": 0},
                                        "fields": {"changed": []}})


def test_local_comprehend_reports_no_fake_scores():
    ents = LocalComprehend().detect_entities("Total 40 USD due May 1, 2026", "en")["Entities"]
    assert {e["Type"] for e in ents} == {"QUANTITY", "DATE"} and all(e["Score"] is None for e in ents)
    assert LocalComprehend().detect_dominant_language("Bonjour")["Languages"] == []


def test_processor_stores_missing_entity_scores_as_null(env):
    env.comprehend.detect_entities = lambda Text, LanguageCode: {"Entities": [
        {"Type": "DATE", "Text": "May 1, 2026", "Score": None}, {"Type": "PERSON", "Text": "Ann", "Score": 0.9}]}
    doc_id = ingest(env, load_app("processor"), "uploads/x/a.pdf", INVOICE_A)
    ents = env.docs.items[(doc_id,)]["entities"]
    assert ents[0]["text"] == "Ann" and ents[1]["score"] is None


def test_verify_rejects_non_integer_expected_version(env):
    doc_id = ingest(env, load_app("processor"), "uploads/x/a.pdf", INVOICE_A)
    code, body = call(load_app("api"), "POST", "/documents/{docId}/fields/{fieldId}/verify",
                      {"docId": doc_id, "fieldId": "f001"}, {"action": "confirm", "expectedVersion": "abc"})
    assert code == 400 and "expectedVersion" in body["error"]


def test_sample_ids_are_deterministic():
    # processor.process(): docId = sha256(f"{bucket}/{key}/{etag}")[:32]
    assert hashlib.sha256(b"local-bucket/uploads/sample_invoice_v1.pdf/e1").hexdigest()[:32] == \
        "98e7e792ab977a95c643e116e6b72d5c"
    assert hashlib.sha256(b"local-bucket/uploads/sample_invoice_v2.pdf/e1").hexdigest()[:32] == \
        "ef241ea578c3e4829afa738f24164ed3"


# ---------------------------------------------------------------- server: routing, fixtures, security
def test_frontend_routes(app):
    r = request(app, "GET", "/")
    assert r.status == 200 and r.headers["content-type"].startswith("text/html") and b"assets/app.js" in r.body
    for legacy in ("/ui", "/ui/", "/ui/index.html", "/index.html"):
        r = request(app, "GET", legacy)
        assert r.status == 307 and r.headers["location"] == "/"
    assert request(app, "GET", "/assets/app.js").status == 200
    assert request(app, "GET", "/assets/app.css").status == 200
    r = request(app, "GET", "/no/such/route")
    assert r.status == 404 and "route not found" in r.json()["error"]


def test_config_and_fixtures_are_labelled(app):
    cfg = request(app, "GET", "/local/config").json()
    assert cfg["mode"] == "local-demo" and cfg["apiKeyRequired"] is False and cfg["acceptedExtensions"] == [".pdf"]
    assert len(cfg["fixtures"]) == 2 and cfg["capabilities"]["intent"]["status"] == "unavailable"
    bucket = os.environ["BUCKET_NAME"]
    want = hashlib.sha256(f"{bucket}/uploads/sample_invoice_v1.pdf/e1".encode()).hexdigest()[:32]
    assert cfg["fixtures"][0]["docId"] == want
    d = request(app, "GET", f"/documents/{want}").json()
    assert d["provenance"]["source"] == "fixture" and d["fileName"].startswith("Demo fixture")
    assert d["summary"] != "An invoice from Acme Cloud."                      # no canned test summary
    assert all(k["grounded"] for k in d["keyDates"])                          # no invented renewal date
    assert d["reviewStatus"] == "PENDING_REVIEW"                              # review demo still works
    assert request(app, "GET", f"/local/documents/{want}/original").status == 404


def test_cross_origin_writes_and_foreign_hosts_are_rejected(app):
    r = request(app, "POST", "/compare", b"{}", {"origin": "https://evil.example", "content-type": "text/plain"})
    assert r.status == 403
    assert request(app, "GET", "/documents", headers={"host": "attacker.example"}).status == 400
    assert request(app, "POST", "/compare", b'{"docIdA":"a","docIdB":"a"}',
                   {"origin": "http://127.0.0.1:8001"}).status == 400               # same origin passes the guard
    assert request(app, "GET", "/documents").headers["access-control-allow-origin"] != "*"


def test_non_utf8_api_body_is_a_400(app):
    assert request(app, "POST", "/compare", b"\xff\xfe\x00").status == 400


def test_upload_validation_without_pymupdf_dependency(make_app):
    app = make_app(max_upload_bytes=1024)
    r = upload(app, b"", "a.pdf")
    assert r.status == 400 and r.json()["code"] == "empty_file"
    r = upload(app, b"x" * 2048, "a.pdf")
    assert r.status == 413 and r.json()["code"] == "too_large"
    r = upload(app, b"\x89PNG....", "scan.png")
    assert r.status == 415 and r.json()["code"] == "ocr_required"
    r = upload(app, b"PK\x03\x04", "notes.docx")
    assert r.status == 415 and r.json()["code"] == "unsupported_type"
    r = upload(app, b"%PDF", "")
    assert r.status == 400


# ---------------------------------------------------------------- server: real PDF processing (PyMuPDF)
def test_upload_real_pdf_end_to_end(app):
    _pymupdf()
    r = upload(app, sample(), "sample_invoice_v1.pdf")
    assert r.status == 201, r.body
    doc = r.json()["document"]
    assert doc["status"] == "COMPLETED" and doc["pageCount"] == 1 and r.json()["duplicate"] is False
    doc_id = doc["docId"]

    listing = request(app, "GET", "/documents").json()["documents"]
    assert doc_id in {d["docId"] for d in listing}

    d = request(app, "GET", f"/documents/{doc_id}").json()
    assert d["provenance"]["source"] == "upload" and d["provenance"]["stages"]["textExtraction"]["status"] == "genuine"
    assert "Northwind" in d["summary"] and "Acme" not in json.dumps(d)       # derived from THIS document
    assert d["keyValues"] == {} and d["avgOcrConfidence"] is None and d["tableCount"] == 0   # no fake OCR output
    # invoice fields come from the deterministic parser, carry no confidence and need review
    assert {f["fieldId"] for f in d["fields"]} >= {"inv_invoiceNumber", "inv_total", "inv_invoiceDate"}
    assert all(f["confidence"] is None and f["source"] == "deterministic-parser" for f in d["fields"])
    assert d["reviewStatus"] == "PENDING_REVIEW"
    assert d["invoice"]["fields"]["total"]["value"] == "4800.00" and d["invoice"]["fields"]["total"]["page"] == 1
    assert d["category"] == "invoice" and d["intent"] == "" and d["actions"] == []
    assert {k["isoDate"] for k in d["keyDates"]} >= {"2026-10-15", "2026-10-20"} and all(k["grounded"] for k in d["keyDates"])
    assert d["classificationMethod"] == "Keyword heuristic (local demo)"

    text = request(app, "GET", f"/local/documents/{doc_id}/text").json()
    assert text["pages"][0]["lines"][0] == "Northwind Cloud Consulting - Invoice"
    orig = request(app, "GET", f"/local/documents/{doc_id}/original")
    assert orig.status == 200 and orig.body == sample()

    again = upload(app, sample(), "renamed.pdf")
    assert again.status == 200 and again.json()["duplicate"] is True and again.json()["document"]["docId"] == doc_id
    assert len(request(app, "GET", "/documents").json()["documents"]) == len(listing)


def test_downstream_features_on_uploaded_pdf(app):
    _pymupdf()
    doc_id = upload(app, sample()).json()["document"]["docId"]
    other = upload(app, sample("sample_invoice_v2.pdf"), "v2.pdf").json()["document"]["docId"]
    D = f"/documents/{doc_id}"

    ans = request(app, "POST", f"{D}/chat", json.dumps({"question": "What is the total amount due?"}).encode()).json()
    assert ans["answerable"] and "4,800 USD" in ans["answer"] and ans["citations"][0]["page"] == 1
    none = request(app, "POST", f"{D}/ask", json.dumps({"question": "Who won the football match?"}).encode()).json()
    assert none["answerable"] is False

    pii = request(app, "POST", f"{D}/pii", b"{}").json()
    assert {"EMAIL", "PHONE", "BANK_ACCOUNT_NUMBER", "ADDRESS"} <= set(pii["counts"]) and "NAME" not in pii["counts"]
    assert "priya.sharma@example.com" not in json.dumps(pii)

    s = request(app, "GET", "/search?q=Northwind").json()
    assert s["searchMode"] == "lexical" and s["results"][0]["docId"] in (doc_id, other)

    cmp = request(app, "POST", "/compare", json.dumps({"docIdA": doc_id, "docIdB": other}).encode()).json()
    assert cmp["aiInterpretation"]["status"] == "FAILED" and cmp["aiInterpretation"]["error"] == "LocalAIUnavailable"
    assert any("5,350 USD" in c["after"] for c in cmp["factualDiff"]["text"]["changed"])

    ex = request(app, "GET", f"{D}/export?format=json").json()
    url = urllib.parse.urlsplit(ex["downloadUrl"])
    assert url.netloc == HOST and url.path.startswith("/local/s3/exports/")
    file = request(app, "GET", f"{url.path}?{url.query}")
    model = json.loads(file.body)
    assert file.status == 200 and "attachment" in file.headers["content-disposition"]
    assert "no AWS AI services" in model["aiInsights"]["generatedBy"] and model["provenance"]["source"] == "upload"
    assert request(app, "GET", f"{url.path}?op=get_object&expires=1").status == 403          # expired link
    assert request(app, "GET", "/local/s3/uploads/x?op=get_object&expires=9999999999").status == 403

    red = request(app, "POST", f"{D}/redact", json.dumps({"entityTypes": ["EMAIL"]}).encode()).json()
    t = urllib.parse.urlsplit(red["downloads"]["text"])
    assert b"[EMAIL]" in request(app, "GET", f"{t.path}?{t.query}").body

    stats = request(app, "GET", "/stats").json()
    assert stats["documents"]["total"] == 4 and stats["documents"]["completed"] == 4


def test_text_free_pdf_is_reported_as_needing_ocr(app):
    pymupdf = _pymupdf()
    doc = pymupdf.open()
    doc.new_page()
    blank = doc.tobytes()
    r = upload(app, blank, "scan.pdf")
    assert r.status == 422 and r.json()["code"] == "ocr_required" and "OCR" in r.json()["error"]
    rec = r.json()["document"]
    assert rec["status"] == "FAILED"
    listed = {d["docId"]: d for d in request(app, "GET", "/documents").json()["documents"]}
    assert listed[rec["docId"]]["status"] == "FAILED"                      # visible, not silently "processed"
    assert upload(app, blank, "scan.pdf").json()["document"]["docId"] == rec["docId"]   # retry, no dup row


def test_damaged_and_protected_pdfs_are_rejected_without_records(app):
    pymupdf = _pymupdf()
    before = len(request(app, "GET", "/documents").json()["documents"])
    r = upload(app, b"%PDF-1.7\n" + os.urandom(64), "broken.pdf")
    assert r.status == 422 and r.json()["code"] == "malformed_pdf"
    r = upload(app, b"just text pretending", "fake.pdf")
    assert r.status == 415 and r.json()["code"] == "not_a_pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    r = upload(app, locked, "locked.pdf")
    assert r.status == 422 and r.json()["code"] == "encrypted_pdf"
    assert len(request(app, "GET", "/documents").json()["documents"]) == before


def test_multi_page_pdf_keeps_page_boundaries(app):
    _pymupdf()
    data = text_pdf([["Alpha agreement between the parties"], ["Beta clause governing law applies"]])
    doc_id = upload(app, data, "two.pdf").json()["document"]["docId"]
    pages = request(app, "GET", f"/local/documents/{doc_id}/text").json()["pages"]
    assert [p["page"] for p in pages] == [1, 2]
    assert any("Beta" in ln for ln in pages[1]["lines"]) and not any("Beta" in ln for ln in pages[0]["lines"])
    hit = request(app, "GET", "/search?q=governing").json()["results"][0]
    assert hit["docId"] == doc_id and hit["snippets"][0]["page"] == 2


def test_presigned_put_flow_emulates_s3(app):
    _pymupdf()
    up = request(app, "POST", "/documents/upload-url", json.dumps({"fileName": "via-put.pdf"}).encode()).json()
    url = urllib.parse.urlsplit(up["uploadUrl"])
    assert url.netloc == HOST and url.path.startswith("/local/s3/uploads/")          # no fake hostname
    r = request(app, "PUT", f"{url.path}?{url.query}", sample("sample_invoice_v2.pdf"), {"content-type": "application/pdf"})
    assert r.status == 200 and r.json()["document"]["fileName"] == "via-put.pdf"
    bad = request(app, "PUT", f"{url.path}?op=get_object&expires=9999999999", b"%PDF-")
    assert bad.status == 403


# ---------------------------------------------------------------- frontend files
@pytest.mark.parametrize("name", ["index.html", "assets/app.css", "assets/app.js"])
def test_frontend_files_are_clean_utf8(name):
    raw = open(os.path.join(ROOT, "frontend", name), "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM"
    text = raw.decode("utf-8")                                         # raises if not UTF-8
    for bad in ("â€", "Ã©", "ðŸ"):      # classic UTF-8-read-as-cp1252 mojibake
        assert bad not in text, f"mojibake {bad!r} in {name}"
    assert "\r\n" not in text


# ---------------------------------------------------------------- evidence-backed answers (Feature B)
SRC = [("S1", "text", "Northwind Cloud Consulting - Invoice\nTotal Amount Due: 4,800 USD\n"
                      "Payment Terms: Net 30. Payment is due by October 15, 2026."),
       ("S2", "field", "Total amount: 4,800 USD"), ("S3", "field", "Due date: October 15, 2026")]


def test_template_answer_cites_the_verbatim_line():
    a = heuristics.answer("What is the total amount due?", SRC)
    assert a["engine"] == "template" and a["answerable"] and a["citations"][0] == "S1"
    assert a["quotes"] == [{"source": "S1", "quote": "Total Amount Due: 4,800 USD"}]
    d = heuristics.answer("When is the payment due?", SRC)
    assert d["engine"] == "template" and "October 15, 2026" in d["answer"]


def test_template_answer_without_source_line_is_low_confidence():
    a = heuristics.answer("What is the total?", [("S1", "field", "Total amount: 4,800 USD")])
    assert a["engine"] == "template" and a["confidence"] == "low" and a["quotes"][0]["source"] == "S1"


def test_extractive_answer_needs_enough_matching_terms():
    srcs = [("S1", "text", "Workloads run on AWS Lambda.\nThe office is closed on Fridays.")]
    a = heuristics.answer("Where do the workloads run?", srcs)
    assert a["engine"] == "extractive" and a["quotes"][0]["quote"] == "Workloads run on AWS Lambda."
    weak = heuristics.answer("Which football teams run the league table standings?", srcs)   # 1 of 5 terms
    assert weak["answerable"] is False and weak["engine"] == "none" and weak["citations"] == []
    assert heuristics.answer("   ", srcs)["answerable"] is False


def test_bedrock_answers_are_labelled_llm(env):
    """Production path (scripted Bedrock reply without an 'engine' key) is reported as an LLM answer."""
    doc_id = ingest(env, load_app("processor"), "uploads/x/a.pdf", INVOICE_A)
    code, body = call(load_app("api"), "POST", "/documents/{docId}/ask", {"docId": doc_id},
                      {"question": "What is the total amount due?"})
    assert code == 200 and body["answerMode"] == "llm"
    assert call(load_app("api"), "POST", "/documents/{docId}/ask", {"docId": "missing1"}, {"question": "x"})[0] == 404


def test_answers_on_uploaded_pdf_are_evidence_backed(app):
    _pymupdf()
    doc_id = upload(app, sample()).json()["document"]["docId"]
    D = f"/documents/{doc_id}"
    r = request(app, "POST", f"{D}/ask", json.dumps({"question": "When is payment due?"}).encode()).json()
    assert r["answerMode"] == "template" and r["answerable"] and "October 15, 2026" in r["answer"]
    assert r["citations"][0]["page"] == 1 and all(q["verified"] for q in r["quotes"])
    r = request(app, "POST", f"{D}/ask", json.dumps({"question": "What is the invoice number?"}).encode()).json()
    assert "NW-2026-001" in r["answer"] and r["answerMode"] == "template"
    r = request(app, "POST", f"{D}/ask", json.dumps({"question": "What is the capital of France?"}).encode()).json()
    assert r["answerable"] is False and r["answerMode"] == "none" and r["citations"] == []
    assert request(app, "POST", f"{D}/ask", json.dumps({"question": ""}).encode()).status == 400


def test_search_snippets_are_not_repeated(app):
    _pymupdf()
    upload(app, sample())
    for res in request(app, "GET", "/search?q=total%20amount%20due").json()["results"]:
        texts = [s["text"] for s in res["snippets"]]
        assert len(texts) == len(set(texts))


# ---------------------------------------------------------------- processing diagnostics + retry (Feature D)
STAGE_ORDER = ["upload", "validation", "textExtraction", "fieldExtraction", "analysis", "indexing", "completion"]


def _audit(app, doc_id, kind):
    return [i for i in app.state.env.app.items.values() if i.get("type") == "AUDIT" and i.get("docId") == doc_id
            and i["eventType"] == kind]


def test_stages_are_recorded_for_uploads_and_fixtures(app):
    _pymupdf()
    doc_id = upload(app, sample()).json()["document"]["docId"]
    st = request(app, "GET", f"/documents/{doc_id}").json()["diagnostics"]["stages"]
    assert [s["stage"] for s in st] == STAGE_ORDER and {s["status"] for s in st} == {"completed"}
    assert all(isinstance(s["durationMs"], int) for s in st[1:]) and st[0]["durationMs"] is None   # not measured
    fx = request(app, "GET", "/local/config").json()["fixtures"][0]["docId"]
    fst = request(app, "GET", f"/documents/{fx}").json()["diagnostics"]["stages"]
    assert [s["status"] for s in fst[:2]] == ["skipped", "skipped"] and "scripted fixture" in fst[2]["detail"]


def test_ocr_failure_is_not_offered_for_retry(app):
    pymupdf = _pymupdf()
    doc = pymupdf.open()
    doc.new_page()
    rec = upload(app, doc.tobytes(), "scan.pdf").json()["document"]
    st = {s["stage"]: s for s in request(app, "GET", f"/documents/{rec['docId']}").json()["diagnostics"]["stages"]}
    assert st["textExtraction"]["status"] == "failed" and st["textExtraction"]["retryable"] is False
    assert "OCR" in st["textExtraction"]["error"] and st["completion"]["status"] == "not_run"
    r = request(app, "POST", f"/local/documents/{rec['docId']}/retry")
    assert r.status == 409 and r.json()["code"] == "not_retryable"


def test_transient_failure_can_be_retried_without_duplicates(app):
    _pymupdf()
    comp = app.state.env.comprehend
    real = comp.detect_entities
    comp.detect_entities = lambda **kw: (_ for _ in ()).throw(RuntimeError("secret internal detail"))
    r = upload(app, sample(), "flaky.pdf")
    assert r.status == 422 and r.json()["code"] == "processing_failed"
    doc_id = r.json()["document"]["docId"]
    st = {s["stage"]: s for s in request(app, "GET", f"/documents/{doc_id}").json()["diagnostics"]["stages"]}
    assert st["analysis"]["status"] == "failed" and st["analysis"]["retryable"] is True
    assert "secret internal detail" not in st["analysis"]["error"] and st["indexing"]["status"] == "not_run"
    count = len(request(app, "GET", "/documents").json()["documents"])

    comp.detect_entities = real
    r = request(app, "POST", f"/local/documents/{doc_id}/retry")
    assert r.status == 200 and r.json()["document"]["docId"] == doc_id and r.json()["document"]["status"] == "COMPLETED"
    d = request(app, "GET", f"/documents/{doc_id}").json()
    assert d["diagnostics"]["attempt"] == 2 and [s["stage"] for s in d["diagnostics"]["stages"]] == STAGE_ORDER
    assert d["provenance"]["source"] == "upload"
    assert len(request(app, "GET", "/documents").json()["documents"]) == count               # no duplicate row
    assert len(_audit(app, doc_id, "DOCUMENT_UPLOADED")) == 1 and len(_audit(app, doc_id, "PROCESSING_RETRIED")) == 1
    assert request(app, "POST", f"/local/documents/{doc_id}/retry").status == 409            # COMPLETED now
    assert request(app, "POST", "/local/documents/doesnotexist/retry").status == 404


def test_duplicate_sqs_delivery_adds_no_audit_events(env):
    from helpers import s3_event
    p = load_app("processor")
    doc_id = ingest(env, p, "uploads/x/a.pdf", INVOICE_A)
    before = len(env.audit_events())
    p.handler(s3_event("uploads/x/a.pdf", "e1"), None)
    assert len(env.audit_events()) == before and env.docs.items[(doc_id,)]["diagnostics"]["attempt"] == 1
