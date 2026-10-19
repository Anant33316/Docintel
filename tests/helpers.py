"""Shared test helpers (imported by conftest.py and the test modules)."""
import importlib.util
import json
import os
import re

from docintel_common import aws
from fakes import FakeBedrock, FakeComprehend, FakeS3, FakeTable, FakeTextract

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_loaded = {}


def load_app(component):
    """Import ``src/<component>/app.py`` under a unique module name (all handlers are called app.py)."""
    if component in _loaded:
        return _loaded[component]
    path = os.path.join(ROOT, "src", component, "app.py")
    spec = importlib.util.spec_from_file_location(f"{component}_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _loaded[component] = mod
    return mod


# ---------------------------------------------------------------- scripted Bedrock
def _last_user_text(messages):
    return messages[-1]["content"][0]["text"]


def default_responder(system, messages):
    text = _last_user_text(messages)
    if system.startswith("You analyze documents"):
        return 'Here you go: {"summary": "An invoice from Acme Cloud.", "intent": "Request payment.", "category": "invoice"}'
    if system.startswith("You extract time-sensitive"):
        doc = text
        out = {"keyDates": [], "actions": []}
        m = re.search(r"Submit the report before ([A-Z][a-z]+ \d{1,2}, \d{4})\.", doc)
        if m:
            out["keyDates"].append({"type": "deadline", "date": m.group(1), "isoDate": "2026-10-20",
                                    "description": "Report submission deadline", "sourceText": m.group(0)})
            out["actions"].append({"action": "Submit report", "deadline": m.group(1), "isoDeadline": "2026-10-20",
                                   "responsibleParty": "Finance team", "sourceText": m.group(0)})
        # a hallucinated item that does not appear in the document -> must be flagged grounded=false
        out["keyDates"].append({"type": "renewal", "date": "March 3, 2027", "isoDate": "2027-02-31",
                                "description": "made up", "sourceText": "This contract renews on March 3, 2027."})
        return json.dumps(out)
    if system.startswith("You answer questions about a single document"):
        q = text.rsplit("Question:", 1)[-1].strip().lower()
        sources = re.findall(r'<source id="(S\d+)" page="([^"]*)" kind="[^"]*">\n(.*?)\n</source>', text, re.S)
        terms = [w for w in re.findall(r"[a-z]{4,}", q) if w not in ("what", "does", "which", "when", "about")]
        for sid, _page, body in sources:
            for line in body.split("\n"):
                if any(t in line.lower() for t in terms):
                    return json.dumps({"answer": f"{line} [{sid}]", "answerable": True, "citations": [sid],
                                       "quotes": [{"source": sid, "quote": line}], "confidence": "high"})
        return json.dumps({"answer": "The document does not contain enough information to answer this question.",
                           "answerable": False, "citations": [], "quotes": [], "confidence": "low"})
    if system.startswith("You compare two documents"):
        b = text.split("<document_b>\n", 1)[1].split("\n</document_b>", 1)[0]
        a = text.split("<document_a>\n", 1)[1].split("\n</document_a>", 1)[0]
        return json.dumps({
            "summary": "B raises the total and adds a late fee.",
            "added": [{"point": "Late fee introduced", "evidenceB": b.split("\n")[-1]}],
            "removed": [],
            "changed": [{"point": "Total changed", "before": a.split("\n")[1], "after": b.split("\n")[1]}],
            "contradictions": [{"point": "Due dates conflict", "docA": "Due in 30 days", "docB": "Due immediately"}],
            "similarities": [{"point": "Same vendor", "evidenceA": a.split("\n")[0], "evidenceB": b.split("\n")[0]}],
        })
    return "{}"


class Env:
    def __init__(self):
        self.docs = FakeTable("docs", "docId")
        self.app = FakeTable("app", "pk", "sk", indexes={"gsi1": ("gsi1pk", "gsi1sk")})
        self.chunks = FakeTable("chunks", "docId", "chunkId")
        self.s3 = FakeS3()
        self.bedrock = FakeBedrock(default_responder)
        self.comprehend = FakeComprehend()
        self.textract = FakeTextract()

    def install(self):
        aws.reset_overrides()
        aws.override("table:docs", self.docs)
        aws.override("table:app", self.app)
        aws.override("table:chunks", self.chunks)
        aws.override("s3", self.s3)
        aws.override("bedrock-runtime", self.bedrock)
        aws.override("comprehend", self.comprehend)
        aws.override("textract", self.textract)

    def uninstall(self):
        aws.reset_overrides()

    def audit_events(self, event_type=None):
        return [i for i in self.app.items.values() if i.get("type") == "AUDIT"
                and (event_type is None or i["eventType"] == event_type)]


def api_event(method, resource, path_params=None, body=None, query=None, headers=None):
    return {"httpMethod": method, "resource": resource, "pathParameters": path_params,
            "queryStringParameters": query, "headers": headers or {},
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"requestId": "req-test"}}


def call(app_module, method, resource, path_params=None, body=None, query=None, headers=None):
    r = app_module.handler(api_event(method, resource, path_params, body, query, headers), None)
    return r["statusCode"], json.loads(r["body"])


def s3_event(key, etag="etag1", bucket=None):
    bucket = bucket or os.environ["BUCKET_NAME"]
    return {"Records": [{"messageId": f"m-{key}", "body": json.dumps(
        {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key, "eTag": etag}}}]})}]}


# ---------------------------------------------------------------- sample documents
from fakes import make_blocks  # noqa: E402

INVOICE_A = {
    "pages": [
        [("Acme Cloud Services Invoice", 99.5), ("Total amount due: 40 USD", 99.1), ("Due in 30 days", 98.0)],
        [("Bill to: John Smith, 12 Baker Street", 97.0), ("Email: john.smith@example.com", 96.0),
         ("Submit the report before October 20, 2026.", 99.0), ("Workloads run on AWS Lambda.", 99.0)],
    ],
    "kvs": [("Invoice Number", "INV-001", 99.0, 98.5, 1), ("Total", "40 USD", 95.0, 62.0, 1),
            ("Account Number", "12345678", 99.0, 99.0, 2)],
    "tables": [[["Item", "Price"], ["Compute", "40"]]],
}
INVOICE_B = {
    "pages": [
        [("Acme Cloud Services Invoice", 99.5), ("Total amount due: 55 USD", 99.0), ("Due in 30 days", 98.0),
         ("Late fee: 5 USD applies after the due date", 97.0)],
    ],
    "kvs": [("Invoice Number", "INV-002", 99.0, 99.0, 1), ("Total", "55 USD", 99.0, 97.0, 1)],
    "tables": [],
}


def ingest(env, processor, key, spec, etag="e1"):
    env.textract.docs[key] = make_blocks(spec["pages"], spec.get("kvs", ()), spec.get("tables", ()))
    out = processor.handler(s3_event(key, etag), None)
    assert out == {"batchItemFailures": []}, out
    return next(i for i in env.docs.items.values() if i["key"] == key)["docId"]
