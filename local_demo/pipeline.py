"""Local upload pipeline: validate bytes -> store in (fake) S3 -> run the REAL processor -> label provenance.

The processor is invoked with the same SQS-wrapped S3 event it receives in AWS, so its production
contract (idempotent claim, status transitions, audit events, chunking, persistence) is exercised
unchanged. Only the AWS clients behind it are local stand-ins (see ``services.py``).
"""
import hashlib
import json
import logging
import re
import time

from docintel_common import aws
from docintel_common.ddb import from_ddb, now_iso, to_ddb

from . import pdf_extract
from .services import IMAGE_EXT, OCR_MESSAGE

logger = logging.getLogger("docintel.local")

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")          # identical to src/api/app.py upload_url()
SUMMARY_FIELDS = ("docId", "fileName", "status", "category", "createdAt", "completedAt", "reviewStatus",
                  "pageCount", "errorMessage")

# ---------------------------------------------------------------- provenance vocabulary
# status: genuine | heuristic | fixture | unavailable
NOTICE = ("Local demo mode: no AWS AI services were used. Text comes from the PDF's embedded text layer; "
          "summary, category, dates, entities and answers are deterministic heuristics, not AI.")


def _stage(status, label):
    return {"status": status, "label": label}


COMMON_STAGES = {
    "invoiceFields": _stage("heuristic", "Deterministic parser: labelled values (\"Invoice Number:\", \"Total Amount "
                                         "Due\", ...), dates and amounts read from the text, plus arithmetic checks. "
                                         "Not AI; vendor and currency may be inferred and are marked so."),
    "summary": _stage("heuristic", "Extractive: the first line plus the most information-dense lines, copied "
                                   "verbatim. Not AI-generated."),
    "category": _stage("heuristic", "Keyword cues (e.g. 'invoice', 'amount due'); 'other' when no category has "
                                    "at least two cues. Not AI."),
    "intent": _stage("unavailable", "Needs a language model (Amazon Bedrock)."),
    "language": _stage("unavailable", "Not detected locally; recorded as the default 'en'."),
    "entities": _stage("heuristic", "Dates and monetary amounts found by pattern matching. No people or "
                                    "organisations, no confidence scores."),
    "keyPhrases": _stage("heuristic", "Most frequent content words in the text."),
    "keyDates": _stage("heuristic", "Dates found by pattern matching; the type is guessed from words on the same "
                                    "line. Every item quotes its source line."),
    "actions": _stage("unavailable", "Required-action extraction needs a language model."),
    "qa": _stage("heuristic", "Extractive: BM25 retrieval, then the single best-matching line is returned. "
                              "No generated answers."),
    "pii": _stage("heuristic", "Pattern matching: emails, phone numbers, SSN-like and account numbers, street "
                               "addresses, Luhn-valid card numbers. Names are not detected."),
    "comparisonAI": _stage("unavailable", "AI comparison needs a language model; the deterministic diff is "
                                          "still computed."),
    "search": _stage("genuine", "BM25 lexical ranking over the extracted text (embeddings disabled)."),
}

UPLOAD_STAGES = {
    "textExtraction": _stage("genuine", "Embedded text layer read with PyMuPDF (no OCR, no layout analysis)."),
    "fields": _stage("heuristic", "Review fields are the invoice values found by the deterministic parser (no "
                                  "confidence scores, so each needs a reviewer). OCR key-value detection needs "
                                  "Amazon Textract."),
    "tables": _stage("unavailable", "Table detection needs Amazon Textract; none extracted."),
    "confidence": _stage("unavailable", "No OCR confidence: text was read directly from the PDF, not recognised."),
    **COMMON_STAGES,
}

FIXTURE_STAGES = {
    "textExtraction": _stage("fixture", "Scripted Textract-style blocks defined in tests/helpers.py. Not read "
                                        "from any PDF file."),
    "fields": _stage("fixture", "Scripted key-value fields from the test fixture."),
    "tables": _stage("fixture", "Scripted table from the test fixture."),
    "confidence": _stage("fixture", "Scripted confidence scores chosen by the fixture to exercise the review "
                                    "workflow; not measured."),
    **COMMON_STAGES,
}


FIXTURE_NOTICE = ("Local demo mode: no AWS AI services were used. This record is a scripted demo fixture: its text, "
                  "fields and confidence scores are test data (not read from a PDF). Summary, category, dates and "
                  "answers are deterministic heuristics over that text, not AI.")


def provenance(source, engine=None):
    stages = json.loads(json.dumps(UPLOAD_STAGES if source == "upload" else FIXTURE_STAGES))
    if engine and source == "upload":
        stages["textExtraction"]["label"] = f"Embedded text layer read with {engine} (no OCR, no layout analysis)."
    return {"source": source, "engine": "local-demo", "notice": NOTICE if source == "upload" else FIXTURE_NOTICE,
            "stages": stages}


def capabilities():
    """Global matrix for the Settings page (same vocabulary as per-document provenance)."""
    return UPLOAD_STAGES


# ---------------------------------------------------------------- errors
class UploadError(Exception):
    def __init__(self, status, code, message, document=None):
        super().__init__(message)
        self.status, self.code, self.message, self.document = status, code, message, document

    def payload(self):
        out = {"error": self.message, "code": self.code}
        if self.document:
            out["document"] = self.document
        return out


def sqs_s3_event(bucket, key, etag):
    """The exact event shape the processor receives in AWS (SQS message wrapping an S3 notification)."""
    return {"Records": [{"messageId": f"local-{etag[:12]}", "body": json.dumps(
        {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key, "eTag": etag}}}]})}]}


class LocalPipeline:
    def __init__(self, processor, s3, textract, max_bytes):
        self.processor = processor
        self.s3 = s3
        self.textract = textract
        self.max_bytes = max_bytes

    # ---- lookups
    def _docs(self):
        return [from_ddb(i) for i in aws.table("docs").scan().get("Items", [])]

    def find(self, **match):
        for d in self._docs():
            if all(d.get(k) == v for k, v in match.items()):
                return d
        return None

    @staticmethod
    def summary(item):
        return {k: item.get(k) for k in SUMMARY_FIELDS if item.get(k) is not None}

    # ---- validation
    def validate_name(self, file_name):
        name = SAFE_NAME.sub("_", (file_name or "").strip())[-100:]
        low = name.lower()
        if not name or name.startswith(".") and name.count(".") == 1:
            raise UploadError(400, "missing_file_name", "A file name ending in .pdf is required.")
        if low.endswith(IMAGE_EXT):
            raise UploadError(415, "ocr_required", "Images need OCR (Amazon Textract), which the local demo does "
                                                   "not include. Upload a PDF that contains selectable text.")
        if not low.endswith(".pdf"):
            raise UploadError(415, "unsupported_type", "Only PDF files are supported in the local demo.")
        return name

    def validate_size(self, size):
        if size == 0:
            raise UploadError(400, "empty_file", "The file is empty (0 bytes).")
        if size > self.max_bytes:
            raise UploadError(413, "too_large", f"The file is {size / 1048576:.1f} MB; the limit is "
                                                f"{self.max_bytes / 1048576:.0f} MB.")

    # ---- ingest
    def ingest(self, file_name, data, key=None):
        """Validate, store and process one upload. Returns ``(http_status, payload)``; raises ``UploadError``."""
        received = {"stage": "upload", "status": "completed", "startedAt": now_iso(), "durationMs": None,
                    "detail": f"{len(data)} bytes received by the local server"}
        name = self.validate_name(file_name)
        self.validate_size(len(data))
        t0 = time.perf_counter()
        try:
            result = pdf_extract.extract(data)
        except pdf_extract.PdfError as e:
            raise UploadError(e.http_status, e.code, e.message)
        validation = {"stage": "validation", "status": "completed", "startedAt": received["startedAt"],
                      "durationMs": int((time.perf_counter() - t0) * 1000),
                      "detail": f"PDF header, structure, password and page limit checked; text layer of "
                                f"{result['pageCount']} page(s) read with {result['engine']}"}

        sha = hashlib.sha256(data).hexdigest()
        existing = next((d for d in self._docs() if d.get("contentSha256") == sha
                         and d.get("status") in ("COMPLETED", "PROCESSING")), None)
        if existing:
            return 200, {"duplicate": True, "document": self.summary(existing),
                         "message": f"This file was already uploaded as '{existing.get('fileName')}'. "
                                    "Showing the existing document instead of creating a duplicate."}

        key = key or f"uploads/{sha[:32]}/{name}"      # same bytes + same name -> same key -> same docId
        bucket = aws.bucket()
        etag = self.s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/pdf")["ETag"]
        self.textract.extracted[key] = result
        return self._run(bucket, key, etag, provenance("upload", result["engine"]), sha, len(data),
                         [received, validation], ocr=result["charCount"] == 0)

    def _run(self, bucket, key, etag, prov, sha, size, pre_stages, ocr=False):
        out = self.processor.handler(sqs_s3_event(bucket, key, etag), None)
        item = self.find(key=key)
        if item is None:     # processor skipped the object (should not happen for .pdf keys)
            raise UploadError(500, "not_processed", "The processor did not create a document record.")
        diag = item.get("diagnostics") or {}
        diag = {**diag, "stages": list(pre_stages) + list(diag.get("stages") or [])}
        self._finalize(item["docId"], prov, sha, size, extra={"diagnostics": diag})
        item = self.find(docId=item["docId"])

        if item.get("status") == "COMPLETED":
            return 201, {"duplicate": False, "document": self.summary(item),
                         "message": f"Processed {item.get('pageCount')} page(s); "
                                    f"{item.get('charCount')} characters of text extracted."}
        code = "ocr_required" if ocr else "processing_failed"
        message = OCR_MESSAGE if code == "ocr_required" else (item.get("errorMessage") or "Processing failed.")
        logger.info("upload %s failed: %s (batch failures: %s)", key, code, out.get("batchItemFailures"))
        raise UploadError(422, code, message, document=self.summary(item))

    def retry(self, doc_id):
        """Re-run processing for a FAILED upload from its stored bytes. Same key + etag -> same docId (no new row)."""
        item = self.find(docId=doc_id)
        if not item:
            raise UploadError(404, "not_found", "document not found")
        if item.get("status") != "FAILED":
            raise UploadError(409, "not_failed", f"Only failed documents can be retried; this one is {item.get('status')}.")
        stages = (item.get("diagnostics") or {}).get("stages") or []
        failed = next((st for st in stages if st.get("status") == "failed"), None)
        if failed and not failed.get("retryable", True):
            raise UploadError(409, "not_retryable", "Retrying would give the same result: " + (failed.get("error") or
                              "the failure depends on the file itself") + " Upload a corrected file instead.")
        obj = self.s3.raw(item.get("bucket"), item.get("key"))
        if not obj:
            raise UploadError(409, "no_file", "The original file is not stored, so it cannot be retried.")
        etag = item.get("etag") or hashlib.md5(obj["Body"]).hexdigest()
        pre = [st for st in stages if st.get("stage") in ("upload", "validation")]
        prov = item.get("provenance") or provenance("upload")
        return self._run(item["bucket"], item["key"], etag, prov, item.get("contentSha256"), item.get("sizeBytes"), pre)

    def _finalize(self, doc_id, prov, sha=None, size=None, extra=None):
        values = {":p": prov, ":cm": "Keyword heuristic (local demo)"}
        expr = "SET provenance = :p, classificationMethod = :cm"
        if sha:
            expr += ", contentSha256 = :sha, sizeBytes = :size"
            values.update({":sha": sha, ":size": size})
        names = {}
        for i, (k, v) in enumerate((extra or {}).items()):
            expr += f", #e{i} = :e{i}"
            names[f"#e{i}"] = k
            values[f":e{i}"] = v
        kw = {"ExpressionAttributeNames": names} if names else {}
        aws.table("docs").update_item(Key={"docId": doc_id}, UpdateExpression=expr,
                                      ExpressionAttributeValues=to_ddb(values), **kw)

    # ---- seeded fixtures
    def seed_fixture(self, key, blocks, display_name, etag="e1"):
        """Process scripted Textract blocks under ``key``; the docId is sha256(bucket/key/etag)[:32] (stable)."""
        self.textract.docs[key] = blocks
        out = self.processor.handler(sqs_s3_event(aws.bucket(), key, etag), None)
        if out != {"batchItemFailures": []}:
            raise RuntimeError(f"fixture {key} failed to process: {out}")
        item = self.find(key=key)
        diag = item.get("diagnostics") or {}
        skipped = [{"stage": "upload", "status": "skipped", "detail": "Seeded fixture: no file was uploaded"},
                   {"stage": "validation", "status": "skipped", "detail": "Seeded fixture: no file to validate"}]
        stages = [dict(st, detail=(st.get("detail") or "") + " (scripted fixture blocks; no file was read)")
                  if st["stage"] == "textExtraction" else st for st in diag.get("stages") or []]
        self._finalize(item["docId"], provenance("fixture"), extra={
            "fileName": display_name, "seedKey": key, "diagnostics": {**diag, "stages": skipped + stages}})
        return item["docId"]
