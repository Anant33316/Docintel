"""Offline stand-ins for the AWS clients, used only by ``local_server.py``.

They are installed through ``docintel_common.aws.override`` exactly like the test fakes, so the
production handlers (processor, API, privacy, export) run unchanged. Each class documents what
it really computes and what it cannot do.

* ``LocalS3``         - in-memory S3 (DynamoDB-strict fakes come from tests/fakes.py); presigned URLs
                        point at this server's ``/local/s3/...`` route instead of an unreachable host.
* ``LocalTextract``   - answers Textract calls from the PDF's embedded text (PyMuPDF): PAGE and LINE
                        blocks only, no KEY_VALUE_SET / TABLE blocks, no confidence. Scripted fixture
                        blocks (the two seeded sample records) are returned as-is.
* ``LocalBedrock``    - no language model. Summary/category/key dates/Q&A are answered with the
                        deterministic heuristics in ``heuristics.py``; anything that would need real
                        generation (AI comparison, intent, required actions, embeddings) raises
                        ``LocalAIUnavailable`` or returns an explicit empty result.
* ``LocalComprehend`` - pattern-based entities / key phrases / PII; no language detection, no names.
"""
import json
import re
import time
import urllib.parse

from docintel_common import grounding

from fakes import FakeS3

from . import heuristics, pdf_extract

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff")


class LocalAIUnavailable(RuntimeError):
    """Raised for operations that genuinely require a generative model."""


class OcrRequired(pdf_extract.PdfError):
    def __init__(self, message):
        super().__init__("ocr_required", message, http_status=422)


OCR_MESSAGE = ("No selectable text was found in this PDF. It is probably a scanned or image-only document, "
               "which needs OCR (for example Amazon Textract in the AWS deployment). The local demo only reads "
               "text that is already embedded in the PDF.")


# ---------------------------------------------------------------- S3
class LocalS3(FakeS3):
    """FakeS3 whose presigned URLs are real, same-machine URLs served by local_server.py."""

    def __init__(self, public_base):
        super().__init__()
        self.public_base = public_base.rstrip("/")

    def generate_presigned_url(self, op, Params, ExpiresIn):
        key = urllib.parse.quote(Params["Key"], safe="/")
        expires = int(time.time()) + int(ExpiresIn)
        return f"{self.public_base}/local/s3/{key}?op={op}&expires={expires}"

    def raw(self, bucket, key):
        return self.objects.get((bucket, key))


# ---------------------------------------------------------------- Textract
class LocalTextract:
    """Textract-compatible facade over PyMuPDF text extraction (text layer only)."""

    def __init__(self, s3):
        self.s3 = s3
        self.docs = {}          # s3 key -> scripted blocks (seeded fixtures), same attribute as FakeTextract
        self.extracted = {}     # s3 key -> pdf_extract.extract() result computed during upload validation
        self._jobs = {}

    def _blocks(self, bucket, key):
        if key in self.docs:
            return self.docs[key]
        if key.lower().endswith(IMAGE_EXT):
            raise OcrRequired("Images need OCR (Amazon Textract), which is not available in the local demo. "
                              "Upload a PDF that contains selectable text.")
        result = self.extracted.pop(key, None)
        if result is None:
            body = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            result = pdf_extract.extract(body)
        if result["charCount"] == 0:
            raise OcrRequired(OCR_MESSAGE)
        return pages_to_blocks(result["pages"])

    def analyze_document(self, Document, FeatureTypes):
        o = Document["S3Object"]
        return {"Blocks": self._blocks(o["Bucket"], o["Name"])}

    def start_document_analysis(self, DocumentLocation, FeatureTypes):
        o = DocumentLocation["S3Object"]
        job = f"local-{len(self._jobs) + 1}"
        self._jobs[job] = self._blocks(o["Bucket"], o["Name"])
        return {"JobId": job}

    def get_document_analysis(self, JobId, NextToken=None):
        return {"JobStatus": "SUCCEEDED", "Blocks": self._jobs.pop(JobId)}


def pages_to_blocks(pages):
    """PAGE + LINE blocks only. No ``Confidence`` key: the processor then records ``None``, not a made-up score."""
    blocks, n = [], 0
    for p, lines in enumerate(pages, start=1):
        n += 1
        blocks.append({"Id": f"p{n}", "BlockType": "PAGE", "Page": p})
        for text in lines:
            n += 1
            blocks.append({"Id": f"l{n}", "BlockType": "LINE", "Text": text, "Page": p})
    return blocks


# ---------------------------------------------------------------- Bedrock
_DOC = re.compile(r"<document>\n(.*)\n</document>", re.S)
_SOURCE = re.compile(r'<source id="(S\d+)" page="([^"]*)" kind="([^"]*)">\n(.*?)\n</source>', re.S)

SUMMARY_PROMPT_PREFIX = "You analyze documents"
KEY_DATES_PROMPT_PREFIX = "You extract time-sensitive"
COMPARE_PROMPT_PREFIX = "You compare two documents"


class LocalBedrock:
    """Answers the processor's / API's Bedrock prompts with deterministic heuristics; never invents content."""

    def __init__(self):
        self.calls = []

    def converse(self, modelId, system, messages, inferenceConfig):
        sys_text = system[0]["text"]
        user = messages[-1]["content"][0]["text"]
        self.calls.append(sys_text[:40])
        if sys_text.startswith(SUMMARY_PROMPT_PREFIX):
            out = self._summary(user)
        elif sys_text.startswith(KEY_DATES_PROMPT_PREFIX):
            out = self._key_dates(user)
        elif sys_text == grounding.SYSTEM_PROMPT:
            out = self._answer(user)
        elif sys_text.startswith(COMPARE_PROMPT_PREFIX):
            raise LocalAIUnavailable("AI comparison needs a language model (Amazon Bedrock); "
                                     "not available in the local demo")
        else:
            raise LocalAIUnavailable("no local equivalent for this prompt")
        return {"output": {"message": {"role": "assistant", "content": [{"text": json.dumps(out)}]}},
                "usage": {"inputTokens": 0, "outputTokens": 0}}       # no model, no tokens

    def invoke_model(self, **kw):
        raise LocalAIUnavailable("embeddings are disabled in the local demo")

    @staticmethod
    def _doc_lines(user):
        m = _DOC.search(user)
        text = m.group(1) if m else user
        return [ln.strip() for ln in text.split("\n") if ln.strip()]

    def _summary(self, user):
        lines = self._doc_lines(user)
        category, _score, _cues = heuristics.classify("\n".join(lines))
        # "intent" requires understanding the document's purpose: left empty (shown as unavailable)
        return {"summary": heuristics.extractive_summary(lines), "intent": "", "category": category}

    def _key_dates(self, user):
        # required actions need semantic understanding: always empty locally
        return {"keyDates": heuristics.key_dates(self._doc_lines(user)), "actions": []}

    def _answer(self, user):
        """Template answer from a matching field passage, else the best-matching line, else 'not found'.

        The reply carries ``engine`` ("template" | "extractive" | "none") so the API can tell the user that
        no language model was involved. Quotes are always copied verbatim from a passage.
        """
        question = user.rsplit("Question:", 1)[-1].strip()
        sources = [(sid, kind, body) for sid, _page, kind, body in _SOURCE.findall(user)]
        return heuristics.answer(question, sources)


# ---------------------------------------------------------------- Comprehend
class LocalComprehend:
    def detect_dominant_language(self, Text):
        return {"Languages": []}          # not detected; the processor then falls back to "en"

    def detect_entities(self, Text, LanguageCode):
        return {"Entities": heuristics.entities(Text)}

    def detect_key_phrases(self, Text, LanguageCode):
        return {"KeyPhrases": heuristics.key_phrases(Text)}

    def detect_pii_entities(self, Text, LanguageCode):
        return {"Entities": heuristics.pii_entities(Text)}

    def classify_document(self, Text, EndpointArn):
        return {"Classes": []}
