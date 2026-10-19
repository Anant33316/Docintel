"""Live end-to-end test against a DEPLOYED stack (real S3/Textract/Comprehend/Bedrock).

Skipped unless both variables are set:
    DOCINTEL_API_URL=https://xxxx.execute-api.us-east-1.amazonaws.com/prod
    DOCINTEL_API_KEY=<api key value>
Run:  pytest tests/integration -s
Costs a few cents per run (2 small PDFs through Textract/Comprehend/Bedrock).
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

API = os.environ.get("DOCINTEL_API_URL", "").rstrip("/")
KEY = os.environ.get("DOCINTEL_API_KEY", "")
HEADERS = {"x-api-key": KEY, "X-User-Id": "integration-test"}

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))


def _call(method, path, body=None, query=""):
    req = urllib.request.Request(f"{API}{path}{query}", method=method, headers={**HEADERS, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _upload(path):
    code, up = _call("POST", "/documents/upload-url", {"fileName": os.path.basename(path)})
    assert code == 200, up
    req = urllib.request.Request(up["uploadUrl"], data=open(path, "rb").read(), method="PUT",
                                 headers={"Content-Type": up["contentType"]})
    urllib.request.urlopen(req, timeout=60).read()
    return os.path.basename(up["key"]), up["key"]


def _wait(file_name, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, listing = _call("GET", "/documents")
        mine = [d for d in listing["documents"] if d["fileName"] == file_name]
        if mine and mine[0]["status"] in ("COMPLETED", "FAILED"):
            return mine[0]
        time.sleep(10)
    raise TimeoutError(file_name)


@pytest.mark.skipif(not (API and KEY), reason="set DOCINTEL_API_URL and DOCINTEL_API_KEY to run live tests")
def test_end_to_end_live():
    from make_sample_docs import main as make_docs
    a_path, b_path = make_docs(tempfile.mkdtemp())
    name_a, _ = _upload(a_path)
    name_b, _ = _upload(b_path)
    a, b = _wait(name_a), _wait(name_b)
    assert a["status"] == "COMPLETED" and b["status"] == "COMPLETED"
    A, B = a["docId"], b["docId"]

    code, doc = _call("GET", f"/documents/{A}")
    assert code == 200 and doc["summary"] and "keyDates" in doc and "fields" in doc

    code, r = _call("POST", f"/documents/{A}/ask", {"question": "What is the total amount due?"})
    assert code == 200 and r["answer"] and "citations" in r

    code, c1 = _call("POST", f"/documents/{A}/chat", {"question": "When is payment due?"})
    assert code == 200 and c1["sessionId"]
    code, c2 = _call("POST", f"/documents/{A}/chat", {"question": "And when must the report be submitted?",
                                                      "sessionId": c1["sessionId"]})
    assert code == 200
    code, hist = _call("GET", f"/documents/{A}/chat/{c1['sessionId']}")
    assert code == 200 and len(hist["messages"]) == 4

    code, fields = _call("GET", f"/documents/{A}/fields")
    assert code == 200 and "summary" in fields
    if fields["fields"]:
        f = fields["fields"][0]
        code, v = _call("POST", f"/documents/{A}/fields/{f['fieldId']}/verify", {"action": "confirm"})
        assert code == 200 and v["field"]["status"] == "VERIFIED"

    code, cmp_ = _call("POST", "/compare", {"docIdA": A, "docIdB": B})
    assert code == 200 and cmp_["factualDiff"]["text"]["changedCount"] + cmp_["factualDiff"]["text"]["addedCount"] > 0

    code, s = _call("GET", "/search", query="?q=AWS%20Lambda")
    assert code == 200 and {r["docId"] for r in s["results"]} >= {A, B}

    code, p = _call("POST", f"/documents/{A}/pii", {})
    assert code == 200 and p["total"] >= 1
    code, red = _call("POST", f"/documents/{A}/redact", {"entityTypes": ["NAME", "EMAIL", "PHONE"]})
    assert code == 200 and red["totalRedacted"] >= 1
    text = urllib.request.urlopen(red["downloads"]["text"], timeout=30).read().decode()
    assert "priya.sharma@example.com" not in text

    for fmt in ("json", "csv", "pdf"):
        code, e = _call("GET", f"/documents/{A}/export", query=f"?format={fmt}")
        assert code == 200, e
        assert urllib.request.urlopen(e["downloadUrl"], timeout=30).read()

    code, stats = _call("GET", "/stats")
    assert code == 200 and stats["documents"]["completed"] >= 2
    code, audit = _call("GET", f"/documents/{A}/audit")
    types = {e["eventType"] for e in audit["events"]}
    assert {"DOCUMENT_PROCESSED", "DOCUMENT_QUERIED", "PII_REDACTED", "EXPORT_GENERATED"} <= types
