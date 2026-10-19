"""DocIntel local demo server: the real DocIntel handlers + offline stand-ins for AWS. No AWS calls.

    python local_server.py          ->  open http://127.0.0.1:8001/

The frontend and the API are served from the same origin, so the browser never needs CORS.

What runs for real
  * every production handler in src/ (processor, API, privacy, export) - unchanged contracts
  * PDF text extraction with PyMuPDF (embedded text layer; no OCR)
  * BM25 search / retrieval, field review workflow, deterministic document diff, exports, audit trail

What is local-only (see local_demo/ and PROJECT_ANALYSIS.md)
  * in-memory S3/DynamoDB (data is lost when the server stops)
  * heuristic stand-ins for Comprehend/Bedrock; no generative AI, clearly labelled in the UI
  * POST /local/upload, the /local/s3/... presigned-URL emulation and a few /local/* helper routes

Configuration (environment variables, all optional)
  DOCINTEL_HOST (127.0.0.1)   DOCINTEL_PORT (8001)   DOCINTEL_MAX_UPLOAD_MB (20)
  DOCINTEL_PUBLIC_URL (http://<host>:<port>)        DOCINTEL_SEED_FIXTURES (1)
  CONFIDENCE_THRESHOLD (90)
"""
import json
import logging
import os
import sys
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(ROOT, "frontend")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")

for _p in (ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "src", "privacy"),
           os.path.join(ROOT, "src", "export"), os.path.join(ROOT, "src", "api"), os.path.join(ROOT, "src", "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HOST = os.environ.get("DOCINTEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("DOCINTEL_PORT", "8001"))
PUBLIC_URL = os.environ.get("DOCINTEL_PUBLIC_URL", f"http://{HOST}:{PORT}").rstrip("/")
MAX_UPLOAD_BYTES = int(float(os.environ.get("DOCINTEL_MAX_UPLOAD_MB", "20")) * 1024 * 1024)

# setdefault (not update): an explicitly configured value - or the test suite's conftest - wins.
for _k, _v in {
    "TABLE_NAME": "docs", "APP_TABLE_NAME": "app", "CHUNKS_TABLE_NAME": "chunks", "BUCKET_NAME": "local-bucket",
    "BEDROCK_MODEL_ID": "local-heuristics", "EMBEDDING_MODEL_ID": "",       # "" = no (fake) embeddings
    "CONFIDENCE_THRESHOLD": "90", "AWS_DEFAULT_REGION": "us-east-1", "CHAT_HISTORY_TTL_DAYS": "30",
    "AUDIT_TTL_DAYS": "365", "CORS_ALLOWED_ORIGIN": PUBLIC_URL,
}.items():
    os.environ.setdefault(_k, _v)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from docintel_common import aws, results  # noqa: E402
from docintel_common.ddb import from_ddb, json_default  # noqa: E402
from docintel_common.http import SAFE_ID  # noqa: E402
from fakes import FakeTable, make_blocks  # noqa: E402
from helpers import INVOICE_A, INVOICE_B, load_app  # noqa: E402
from local_demo import pdf_extract  # noqa: E402
from local_demo.pipeline import NOTICE, LocalPipeline, UploadError, capabilities  # noqa: E402
from local_demo.services import LocalBedrock, LocalComprehend, LocalS3, LocalTextract  # noqa: E402

logger = logging.getLogger("docintel.local")

# The two seeded records. Key + etag are unchanged from the original local server, so their docIds stay
# sha256("local-bucket/<key>/e1")[:32] = 98e7e792... and ef241ea5... on every start.
FIXTURES = [
    ("uploads/sample_invoice_v1.pdf", INVOICE_A, "Demo fixture - Acme invoice A"),
    ("uploads/sample_invoice_v2.pdf", INVOICE_B, "Demo fixture - Acme invoice B"),
]
DOWNLOADABLE_PREFIXES = ("exports/", "redacted/")


class LocalEnv:
    """All in-memory state of one server instance."""

    def __init__(self, public_url):
        self.docs = FakeTable("docs", "docId")
        self.app = FakeTable("app", "pk", "sk", indexes={"gsi1": ("gsi1pk", "gsi1sk")})
        self.chunks = FakeTable("chunks", "docId", "chunkId")
        self.s3 = LocalS3(public_url)
        self.textract = LocalTextract(self.s3)
        self.bedrock = LocalBedrock()
        self.comprehend = LocalComprehend()

    def install(self):
        aws.reset_overrides()
        for name, obj in (("table:docs", self.docs), ("table:app", self.app), ("table:chunks", self.chunks),
                          ("s3", self.s3), ("textract", self.textract), ("bedrock-runtime", self.bedrock),
                          ("comprehend", self.comprehend)):
            aws.override(name, obj)


def _allowed(public_url, port):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", urllib.parse.urlsplit(public_url).netloc}
    origins = {f"http://{h}" for h in hosts} | {public_url}
    return hosts, origins


def match_route(handlers, method, path):
    """Find the handler module + API Gateway resource template for ``method path``."""
    path_parts = path.strip("/").split("/")
    for module in handlers:
        for route_method, template in module.ROUTES:
            if route_method != method:
                continue
            template_parts = template.strip("/").split("/")
            if len(template_parts) != len(path_parts):
                continue
            params = {}
            for expected, actual in zip(template_parts, path_parts):
                if expected.startswith("{") and expected.endswith("}"):
                    params[expected[1:-1]] = urllib.parse.unquote(actual)
                elif expected != actual:
                    break
            else:
                return module, template, params
    return None, None, {}


def _json(status, body):
    return Response(json.dumps(body, default=json_default), status_code=status, media_type="application/json")


def create_app(seed=True, public_url=PUBLIC_URL, port=PORT, max_upload_bytes=MAX_UPLOAD_BYTES):
    env = LocalEnv(public_url)
    env.install()
    lock = threading.Lock()       # the in-memory tables are not thread-safe: one handler at a time
    processor = load_app("processor")
    pipeline = LocalPipeline(processor, env.s3, env.textract, max_upload_bytes)
    handlers = [load_app("api"), load_app("privacy"), load_app("export")]   # as wired in template.yaml

    fixture_ids = []
    if seed and os.environ.get("DOCINTEL_SEED_FIXTURES", "1") != "0":
        for key, spec, label in FIXTURES:
            blocks = make_blocks(spec["pages"], spec.get("kvs", ()), spec.get("tables", ()))
            fixture_ids.append({"docId": pipeline.seed_fixture(key, blocks, label), "fileName": label})

    def locked(fn, *a):
        with lock:
            return fn(*a)

    app = FastAPI(title="DocIntel local demo", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.env, app.state.pipeline, app.state.fixtures = env, pipeline, fixture_ids
    allowed_hosts, allowed_origins = _allowed(public_url, port)

    @app.middleware("http")
    async def local_only_guard(request: Request, call_next):
        # DNS-rebinding guard: only answer requests addressed to this machine.
        if request.headers.get("host") not in allowed_hosts:
            return _json(400, {"error": "invalid Host header"})
        # CSRF guard: other websites open in the browser must not change local data.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in allowed_origins:
            return _json(403, {"error": "cross-origin request rejected"})
        return await call_next(request)

    # ---------------------------------------------------------------- frontend (registered first)
    @app.get("/", include_in_schema=False)
    def frontend():
        return FileResponse(INDEX_HTML, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    @app.get("/ui/index.html", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def legacy_ui():
        return RedirectResponse("/", status_code=307)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")

    # ---------------------------------------------------------------- local-only routes
    @app.get("/local/config")
    def local_config():
        return {
            "mode": "local-demo",
            "apiKeyRequired": False,
            "notice": NOTICE,
            "maxUploadBytes": max_upload_bytes,
            "acceptedExtensions": [".pdf"],
            "pdfExtraction": {"available": pdf_extract.available(),
                              "engine": f"PyMuPDF {pdf_extract.engine_version()}" if pdf_extract.available() else None},
            "confidenceThreshold": float(os.environ.get("CONFIDENCE_THRESHOLD", "90")),
            "fixtures": fixture_ids,
            "capabilities": capabilities(),
            "storage": "in-memory (cleared when the server stops)",
        }

    @app.post("/local/upload")
    async def local_upload(request: Request):
        file_name = request.query_params.get("fileName") or ""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_upload_bytes:
            return _json(413, {"error": f"The file is larger than the {max_upload_bytes // 1048576} MB limit.",
                               "code": "too_large"})
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > max_upload_bytes:
                return _json(413, {"error": f"The file is larger than the {max_upload_bytes // 1048576} MB limit.",
                                   "code": "too_large"})
        try:
            status, payload = await run_in_threadpool(locked, pipeline.ingest, file_name, bytes(data))
        except UploadError as e:
            return _json(e.status, e.payload())
        except Exception:
            logger.exception("unexpected upload failure")
            return _json(500, {"error": "Unexpected server error while processing the upload. See the server "
                                        "console for details.", "code": "internal_error"})
        return _json(status, payload)

    def _get_doc(doc_id):
        if not SAFE_ID.match(doc_id or ""):
            return None, _json(400, {"error": "invalid docId"})
        item = env.docs.get_item(Key={"docId": doc_id}).get("Item")
        if not item:
            return None, _json(404, {"error": "document not found"})
        return from_ddb(item), None

    @app.get("/local/documents/{doc_id}/text")
    def local_text(doc_id: str):
        with lock:
            item, err = _get_doc(doc_id)
            if err:
                return err
            try:
                data = results.load(item)
            except results.NotReady as e:
                return _json(409, {"error": str(e)})
            stage = ((item.get("provenance") or {}).get("stages") or {}).get("textExtraction")
            pages = [{"page": p.get("page"), "lines": [ln.get("text") for ln in p.get("lines", [])]}
                     for p in data["pageLines"]]
            return {"docId": doc_id, "pages": pages, "charCount": len(data.get("text", "")), "extraction": stage}

    @app.get("/local/documents/{doc_id}/original")
    def local_original(doc_id: str):
        with lock:
            item, err = _get_doc(doc_id)
            if err:
                return err
            obj = env.s3.raw(item.get("bucket"), item.get("key"))
        if not obj:
            return _json(404, {"error": "No original file is stored for this record (seeded demo fixtures are "
                                        "scripted data, not uploaded files)."})
        name = os.path.basename(item.get("key") or "document.pdf")
        return Response(obj["Body"], media_type=obj.get("ContentType") or "application/octet-stream",
                        headers={"Content-Disposition": f'inline; filename="{name}"'})

    @app.post("/local/documents/{doc_id}/retry")
    async def local_retry(doc_id: str):
        if not SAFE_ID.match(doc_id or ""):
            return _json(400, {"error": "invalid docId"})
        try:
            status, payload = await run_in_threadpool(locked, pipeline.retry, doc_id)
        except UploadError as e:
            return _json(e.status, e.payload())
        return _json(200 if status in (200, 201) else status, payload)

    def _check_presigned(request, op):
        if request.query_params.get("op") != op:
            return _json(403, {"error": "URL is not valid for this operation"})
        try:
            if int(request.query_params.get("expires", "0")) < time.time():
                return _json(403, {"error": "This link has expired. Generate a new one."})
        except ValueError:
            return _json(403, {"error": "invalid link"})
        return None

    @app.get("/local/s3/{key:path}")
    def local_s3_get(key: str, request: Request):
        err = _check_presigned(request, "get_object")
        if err:
            return err
        if not key.startswith(DOWNLOADABLE_PREFIXES):
            return _json(403, {"error": "object is not downloadable"})
        with lock:
            obj = env.s3.raw(aws.bucket(), key)
        if not obj:
            return _json(404, {"error": "file not found (in-memory files are lost when the server restarts)"})
        headers = {}
        if obj.get("ContentDisposition"):
            headers["Content-Disposition"] = obj["ContentDisposition"]
        return Response(obj["Body"], media_type=obj.get("ContentType") or "application/octet-stream",
                        headers=headers)

    @app.put("/local/s3/{key:path}")
    async def local_s3_put(key: str, request: Request):
        """Emulates the presigned S3 PUT returned by POST /documents/upload-url (used by scripts/demo.py)."""
        err = _check_presigned(request, "put_object")
        if err:
            return err
        if not key.startswith("uploads/"):
            return _json(403, {"error": "uploads only"})
        data = await request.body()
        try:
            status, payload = await run_in_threadpool(locked, pipeline.ingest, os.path.basename(key), data, key)
        except UploadError as e:
            return _json(e.status, e.payload())
        return _json(200 if status in (200, 201) else status, payload)

    # ---------------------------------------------------------------- API Gateway emulation (registered last)
    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    async def api_gateway(path: str, request: Request):
        actual_path = "/" + path
        module, resource, path_params = match_route(handlers, request.method, actual_path)
        if resource is None:
            return _json(404, {"error": f"route not found: {request.method} {actual_path}"})
        raw = await request.body()
        try:
            body = raw.decode("utf-8") if raw else None
        except UnicodeDecodeError:
            return _json(400, {"error": "request body must be UTF-8 JSON"})
        event = {
            "httpMethod": request.method,
            "resource": resource,
            "path": actual_path,
            "headers": dict(request.headers),
            "queryStringParameters": dict(request.query_params) or None,
            "pathParameters": path_params,
            "body": body,
            "requestContext": {"requestId": f"local-{time.time_ns()}"},
        }
        result = await run_in_threadpool(locked, module.handler, event, None)
        headers = {k: v for k, v in (result.get("headers") or {}).items()
                   if k.lower() not in ("content-length", "content-type")}
        return Response(result.get("body") or "{}", status_code=result.get("statusCode", 200),
                        media_type="application/json", headers=headers)

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("audit").setLevel(logging.WARNING)       # structured CloudWatch-style lines: too noisy here
    if not pdf_extract.available():
        print("WARNING: PyMuPDF is not installed - PDF uploads will be rejected. pip install pymupdf==1.28.2")
    app = create_app()
    print("DocIntel local demo (in-memory; no AWS calls)")
    print("PDF text extraction:", f"PyMuPDF {pdf_extract.engine_version()}" if pdf_extract.available() else "UNAVAILABLE")
    for f in app.state.fixtures:
        print(f"Seeded fixture: {f['docId']}  ({f['fileName']})")
    print(f"Open {PUBLIC_URL}/ in your browser  (Ctrl+C to stop)")
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
