# DocIntel v2 upgrade report

This document is the delivery report for the v2 upgrade of *"Design of a Serverless AI-Powered Document Intelligence Architecture on the Cloud"*. The existing v1 architecture (S3 → SQS → Processor Lambda → Textract/Comprehend/Bedrock → DynamoDB → API Gateway/API Lambda) was kept; every v1 route keeps its request/response contract.

## 0. What already existed (inspection results)

| Requested feature | State in v1 | What v2 did |
|---|---|---|
| Q&A (F2, F9) | Single-turn `/ask`; whole document (first 30k chars) stuffed into the prompt; no citations, no history | Retrieval of relevant chunks, numbered sources with pages, citation/quote validation, chat sessions with history. `/ask` reuses the same engine. |
| Confidence (F3) | Textract `Confidence` discarded; Comprehend scores discarded | Kept for lines, key/value blocks and entities; field confidence + threshold + review status |
| Entities / key phrases | Present (no scores) | Scores added |
| Dashboard (F6) | CloudWatch infra dashboard only | `/stats` API + console dashboard; CloudWatch dashboard extended |
| Logging / audit (F11) | Application logs only | Content-free audit trail in DynamoDB + structured CloudWatch lines + failure alarm |
| Intent / summary / category | Present | Unchanged (category method now recorded) |
| Comparison, PII, search, key dates, export, human review, frontend | Not present | Added |

## 1. Modified files

| File | Change |
|---|---|
| `template.yaml` | New parameters, shared layer, 2 DynamoDB tables, 2 Lambdas, 18 new API routes, CORS + gateway responses, lifecycle rules, scoped Bedrock IAM, new alarms, metric filters and dashboard widgets, new outputs. All v1 resources kept with the same logical IDs. |
| `src/processor/app.py` | Uses shared layer; keeps confidence (fields/lines), entity scores, key-date/action extraction, review flags, chunk + embedding indexing, processing time + AI usage stats, classification method, audit events. Handler, idempotency claim, Textract polling and v1 outputs unchanged. |
| `src/api/app.py` | Router now delegates to feature modules; v1 handlers kept (`upload_url`, `list_docs`, `get_doc`); `/ask` upgraded to the grounded engine with the same `question`/`answer` keys. |
| `scripts/demo.py` | Same usage; prints citations; new `--tour` flag walks through v2 features. |
| `tests/test_parse.py` | Only change: environment defaults use `setdefault` so the shared test environment isn't overwritten. Assertions unchanged and passing. |
| `README.md` | Rewritten for v2 (deploy, upgrade path, API table, tests, limits). |
| `.gitignore` | Adds `.pytest_cache/`. |

## 2. New files

| Path | Purpose |
|---|---|
| `src/shared/docintel_common/aws.py` | Lazy, cached AWS clients/tables; test override hooks; thread-safe table factory |
| `src/shared/docintel_common/ddb.py` | Decimal/float conversion, pagination, time/TTL helpers |
| `src/shared/docintel_common/bedrock.py` | Converse wrapper (returns token usage), JSON extraction, Titan embeddings, vector packing |
| `src/shared/docintel_common/chunking.py` | Page-aware chunking, offset→page mapping |
| `src/shared/docintel_common/retrieval.py` | Tokenizer, BM25, cosine, hybrid ranking, snippets |
| `src/shared/docintel_common/grounding.py` | Grounded-answer prompt, citation/quote validation, "not enough information" handling |
| `src/shared/docintel_common/diffing.py` | Deterministic line + field diff |
| `src/shared/docintel_common/pii.py` | Comprehend PII detection (chunked under the 100 KB limit), masking, range-based redaction |
| `src/shared/docintel_common/review.py` | Threshold, field review states, review summary |
| `src/shared/docintel_common/results.py` | Load `results/` JSON with v1 backward compatibility |
| `src/shared/docintel_common/audit.py` | Audit events (DynamoDB + CloudWatch), sanitiser, dashboard counters |
| `src/shared/docintel_common/http.py` | API Gateway helpers, router, errors, caller identity |
| `src/shared/docintel_common/__init__.py`, `src/shared/requirements.txt` | Layer package |
| `src/api/qa.py` | `/ask` + chat sessions |
| `src/api/review_api.py` | Fields, verification, verification trail |
| `src/api/comparison.py` | Comparison (factual + AI), cache, listings |
| `src/api/search.py` | Cross-document hybrid search with filters |
| `src/api/insights.py` | `/stats`, audit queries |
| `src/privacy/app.py` | PII detection + redaction function |
| `src/export/app.py`, `formats.py`, `pdf_report.py` | Export function, JSON/CSV/PDF renderers, dependency-free PDF writer |
| `frontend/index.html` | Browser console (all features) |
| `scripts/make_sample_docs.py` | Generates two related sample invoice PDFs (fictional data) |
| `tests/fakes.py`, `tests/helpers.py`, `tests/conftest.py` | Strict in-memory AWS fakes and fixtures |
| `tests/test_shared.py`, `test_processor.py`, `test_api.py`, `test_privacy_export.py`, `test_template.py` | Unit + integration tests (61 tests) |
| `tests/integration/test_live_api.py` | Live end-to-end test against a deployed stack (opt-in) |
| `docs/API.md`, `docs/ARCHITECTURE.md`, `docs/architecture.svg`, `docs/UPGRADE_REPORT.md` | Documentation |
| `pytest.ini`, `requirements-dev.txt` | Test configuration |

## 3. Feature explanations

1. **Comparison** — `POST /compare`. Loads both extractions; `difflib` produces the factual diff (added/removed/changed lines — a line counts as "changed" only if ≥60 % similar — plus extracted-field changes). Bedrock produces summary, added, removed, changed, contradictions and similarities, each with evidence quotes that are checked against the source text (`verified`). The two parts are separate keys (`factualDiff` vs `aiInterpretation`, labelled `generatedBy: LLM` with a disclaimer). Stored in DynamoDB and cached by (A, B, model).
2. **Chat history** — `POST /documents/{id}/chat` creates/continues a session owned by `X-User-Id`; messages (with citations) are stored in DynamoDB with a TTL; the last 3 exchanges are sent to Bedrock; only the top-ranked passages for the current (+ previous) question are retrieved.
3. **Confidence scoring** — Textract confidence is kept for every line and form field (field = min of key and value block confidence) and Comprehend entity scores are kept. `ConfidenceThreshold` (default 90) marks fields `REQUIRES_REVIEW`; the API can re-evaluate with `?threshold=`.
4. **PII** — Privacy Lambda: detection mode returns types, scores, page/location and masked previews (no raw values anywhere); redaction writes a new `redacted/…` JSON + text copy (lines, field keys/values, table cells), records metadata (types, counts, S3 keys, SHA-256) and an audit event. The original upload and `results/` are never written (IAM forbids it).
5. **Search** — `GET /search` ranks chunks + document metadata with BM25, blended with Titan-embedding cosine when enabled; returns ranked documents with page-tagged snippets and matched terms; filters by category, status and upload date range.
6. **Dashboard** — `GET /stats` aggregates the Documents table and atomic counters: totals by status, categories, processing time (avg/p50/p90/max), recent failures and uploads, uploads per day, AI usage (Textract pages, Comprehend calls, Bedrock tokens, questions, comparisons, searches, PII scans, exports, verifications), review backlog, upcoming key dates. The console renders it; the CloudWatch dashboard covers infrastructure.
7. **Human-in-the-loop** — `POST …/fields/{fieldId}/verify` with `confirm` or `correct`. Corrections go to `verifications.{fieldId}`; the AI value is never changed; every decision appends an immutable record (original value, previous correction, new value, status, timestamp, reviewer, note, version). Optimistic locking prevents lost updates.
8. **Key dates and actions** — Bedrock extracts deadlines, expirations, renewals, effective/payment dates, events and actions with deadlines and responsible parties. Each item quotes its source sentence; `grounded` is true only if that sentence is in the document; ISO dates are validated (invalid → null).
9. **Source-grounded answers** — every answer carries `citations` (document, page, chunk, snippet, relevance, OCR confidence), verified `quotes`, `answerable`, `confidence` and `warnings`. Unanswerable → explicit "The document does not contain enough information…".
10. **Export** — `GET …/export?format=json|csv|pdf` writes to `exports/` and returns a 5-minute link. Includes metadata, extracted text, fields (AI value, confidence, status, corrections), classification, summary, intent, entities, key phrases, key dates, actions, tables, PII summary and AI stats.
11. **Audit logging** — `audit.record()` writes a DynamoDB event (per document + per day via GSI, TTL) and a structured CloudWatch line. Events: uploaded, processed, failed, queried, compared, searched, PII detected/redacted, result modified, field verified, export generated (+ upload URL issued, chat deleted). A sanitiser drops content-bearing keys; failures raise the `AuditWriteFailures` alarm.

## 4. Updated architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`architecture.svg`](architecture.svg). Summary: same event-driven ingestion; the processor additionally builds a retrieval index; three API-facing Lambdas share one layer; data in one S3 bucket (prefixes) and three DynamoDB tables.

## 5. New AWS resources

| Logical ID | Type | Why |
|---|---|---|
| `SharedLayer` | Lambda layer | One copy of shared code |
| `AppTable` (+ GSI `gsi1`, TTL) | DynamoDB | Chat, comparisons, verifications, redactions, audit, counters |
| `ChunksTable` | DynamoDB | Retrieval/search index |
| `PrivacyFunction`, `PrivacyLogs` | Lambda + log group | Isolated PII permissions |
| `ExportFunction`, `ExportLogs` | Lambda + log group | Exports |
| 18 API routes (+ CORS OPTIONS) | API Gateway | New endpoints |
| `PrivacyErrorsAlarm`, `ExportErrorsAlarm`, `AuditWriteFailuresAlarm` | CloudWatch alarms | Monitoring |
| `AuditFailures*Filter` (×4) | Log metric filters | Audit write failure metric |
| S3 lifecycle rules `expire-exports`, `expire-redacted` | (bucket property) | Cost/privacy hygiene |

No OpenSearch, Kendra, Step Functions, Cognito, VPC or NAT were added.

## 6. New API endpoints

`POST /documents/{docId}/chat`, `GET /documents/{docId}/chat`, `GET|DELETE /documents/{docId}/chat/{sessionId}`, `GET /documents/{docId}/fields`, `POST /documents/{docId}/fields/{fieldId}/verify`, `GET /documents/{docId}/verifications`, `POST /compare`, `GET /comparisons/{comparisonId}`, `GET /documents/{docId}/comparisons`, `GET /search`, `GET /stats`, `GET /documents/{docId}/audit`, `GET /audit`, `POST /documents/{docId}/pii`, `POST /documents/{docId}/redact`, `GET /documents/{docId}/redactions`, `GET /documents/{docId}/export`. Upgraded: `POST /documents/{docId}/ask`. Details and samples: [`API.md`](API.md).

## 7. DynamoDB schema changes

* **Documents** (key unchanged): new attributes `fields`, `verifications`, `confidenceThreshold`, `reviewStatus`, `reviewSummary`, `reviewVersion`, `keyDates`, `actions`, `chunkCount`, `processingMs`, `aiStats`, `avgOcrConfidence`, `classificationMethod`, `piiSummary`, `failedAt`; `entities[].score`. No migration needed.
* **Chunks** (new): `docId` (HASH), `chunkId` (RANGE).
* **App** (new): `pk`/`sk`, GSI `gsi1` (`gsi1pk`/`gsi1sk`), TTL `expiresAt`. Item types listed in [`ARCHITECTURE.md`](ARCHITECTURE.md#dynamodb-app-new--single-table-store).

## 8. Environment variables

| Variable | Set from | Used by |
|---|---|---|
| `TABLE_NAME`, `BUCKET_NAME`, `BEDROCK_MODEL_ID` | existing | all |
| `APP_TABLE_NAME`, `CHUNKS_TABLE_NAME` | new tables | all |
| `EMBEDDING_MODEL_ID` | `EmbeddingModelId` (empty = disabled) | processor, API |
| `CONFIDENCE_THRESHOLD` | `ConfidenceThreshold` | processor, API, export |
| `CHAT_HISTORY_TTL_DAYS` | `ChatHistoryTtlDays` | API |
| `AUDIT_TTL_DAYS` | `AuditRetentionDays` | all |
| `CORS_ALLOWED_ORIGIN` | `CorsAllowedOrigin` | API-facing functions |
| `CLASSIFIER_ENDPOINT_ARN` | existing | processor |
| `BEDROCK_READ_TIMEOUT` | 120 (processor), default 25 elsewhere | Bedrock client |
| Optional tuning (code defaults): `QA_TOP_K` (6), `COMPARE_MAX_CHARS` (12000), `MAX_SEARCH_CHUNKS` (20000) | — | API |

No credentials or secrets are configured anywhere; Lambdas use their IAM roles.

## 9. Deployment commands

```bash
# one-time: enable Bedrock model access for your LLM and Titan Text Embeddings V2
sam build
sam deploy --guided --stack-name docintel \
  --parameter-overrides BedrockModelId=anthropic.claude-3-haiku-20240307-v1:0 AlertEmail=you@example.com \
                        EmbeddingModelId=amazon.titan-embed-text-v2:0 ConfidenceThreshold=90
aws cloudformation describe-stacks --stack-name docintel --query "Stacks[0].Outputs"
aws apigateway get-api-key --api-key <ApiKeyId> --include-value --query value --output text
# later updates:
sam build && sam deploy
# teardown:
aws s3 rm s3://<BucketName> --recursive && sam delete --stack-name docintel
```

## 10. Testing commands

```bash
pip install -r requirements-dev.txt
pytest                                   # 61 offline tests (unit + handler-level integration with AWS fakes)
pytest tests/test_template.py            # template ↔ code consistency + IAM boundaries
cfn-lint template.yaml && sam validate --lint
python scripts/make_sample_docs.py samples/
python scripts/demo.py <ApiUrl> <key> samples/sample_invoice_v1.pdf "What is the total amount due?" --tour
DOCINTEL_API_URL=<ApiUrl> DOCINTEL_API_KEY=<key> pytest tests/integration -s     # live, ~cents per run
```

## 11–12. Example requests and responses

Every endpoint has a curl example and a response captured from the real handlers in [`API.md`](API.md).

## 13. Known limitations

* **Not verified against real AWS in this delivery.** The code was exercised end-to-end against strict in-memory fakes (61 tests, Python 3.12 and 3.13) and the browser console was driven through every feature against the real handlers. `sam build`, `cfn-lint`/`sam validate` and the live integration test could not be run in the build environment (no AWS account; package index unreachable), so run them first; see §10.
* **User identity** is the caller-asserted `X-User-Id` header behind a shared API key. It separates chat sessions and attributes audit events but is not authentication. Add a Cognito user-pool authorizer and derive the user from the token for real multi-user use.
* **Redaction is text-level.** No visually redacted PDF/image of the original file is produced (that would need Textract word geometry + image/PDF rendering).
* **Search scale**: scans DynamoDB chunks per query (≈20k chunks practical ceiling, a few seconds at the top end). Upgrade path: OpenSearch Serverless or a Bedrock Knowledge Base fed from `results/`.
* **LLM limits**: comparison sends ≤12k characters per document to Bedrock (the factual diff uses full text; `truncated` flags it); key dates/summary use the first 30k characters; chat sends ≤12k characters of passages. Verification catches fabricated quotes, not misreadings of real quotes.
* **Confidence** exists only for Textract output (fields/lines) and Comprehend entities; Bedrock outputs have no calibrated confidence (answers expose self-reported confidence lowered by our checks).
* **Pages**: images are single-page; documents processed by v1 have no page numbers (citations show `page: null`) until re-uploaded.
* **PDF export** uses the base-14 Helvetica font: characters outside Windows-1252 appear as `?` (JSON/CSV keep full Unicode).
* **API Gateway 29 s limit**: very large comparisons/chats can time out; the Bedrock client gives up at 25 s and the factual diff still returns for comparisons.
* **Audit is best-effort** (never blocks the user action); failures are logged and alarmed.
* `/documents`, `/stats` and search use scans: fine for a project-sized dataset.

## 14. Cost considerations (approximate list prices, us-east-1 — check the AWS Pricing Calculator for your region and current prices)

| Item | Driver | Rough cost |
|---|---|---|
| Textract AnalyzeDocument (forms + tables) | per page | ≈ $0.065 / page — **dominant cost** (free tier for new accounts covers some pages per month) |
| Comprehend (language, entities, key phrases) | 3 calls × ≤4.5k chars | ≈ $0.01–0.02 / document |
| Comprehend PII (on demand only) | per scan/redaction, 100-char units | ≈ $0.01 per 10k characters |
| Bedrock Claude 3 Haiku | summary + key dates per doc; per question; per comparison | ≈ $0.005 / document, ≈ $0.001 / question, ≈ $0.003 / comparison |
| Titan Text Embeddings V2 | per chunk + per query | ≈ $0.02 per million tokens (negligible) |
| Lambda, API Gateway, SQS, DynamoDB on-demand, S3 | per request / GB | cents per month at project scale; mostly within free tier |
| CloudWatch | 6 alarms, 1 dashboard, 1 custom metric, logs | ≈ $1–4 / month (some covered by free tier) |

Example: 200 two-page documents + 1,000 questions + 50 comparisons per month ≈ $26 Textract + $3 Comprehend + $3 Bedrock + ~$2 other ≈ **$35 / month**; idle ≈ $0 (+ alarms/dashboard). Cost guards: processor concurrency 5, API throttling + 5,000 requests/day quota, comparison cache, PII only on demand, exports expire after 7 days, no always-on search cluster. Set `EmbeddingModelId=""` to skip embeddings entirely.

## 15. Requirement checklist

| Requirement | Status | Notes |
|---|---|---|
| **F1** extract content from both docs | COMPLETE | |
| F1 deterministic comparison | COMPLETE | line + field diff |
| F1 Bedrock semantic comparison | COMPLETE | |
| F1 added / removed / changed / contradictory / similarities | COMPLETE | |
| F1 concise summary | COMPLETE | |
| F1 stored in DynamoDB + API | COMPLETE | |
| F1 facts separated from LLM interpretation | COMPLETE | `factualDiff` vs `aiInterpretation` + verified flags |
| **F2** history per document and session | COMPLETE | |
| F2 history per *user* | PARTIALLY COMPLETE | user = caller-asserted `X-User-Id`, not authenticated |
| F2 multiple questions, DynamoDB storage, Bedrock | COMPLETE | |
| F2 retrieve only relevant context | COMPLETE | BM25 (+ embeddings) top-k ≤12k chars |
| F2 source/page references, no invented info | COMPLETE | validated citations, explicit refusal |
| **F3** preserve AWS confidence scores | COMPLETE | Textract + Comprehend |
| F3 low-confidence identification, REQUIRES_REVIEW, configurable threshold | COMPLETE | stack parameter + per-request override |
| F3 stored in DynamoDB, exposed via API | COMPLETE | |
| **F4** PII detection mode | COMPLETE | masked previews |
| F4 redacted version of document *text* | COMPLETE | |
| F4 redacted version of the original *file* (image/PDF) | NOT IMPLEMENTED | text-level redaction only |
| F4 original never overwritten, separate S3 output, metadata | COMPLETE | enforced by IAM |
| F4 least-privilege IAM | COMPLETE | dedicated function; asserted in tests |
| **F5** search metadata + content, snippets, ranking, filters, API | COMPLETE | |
| F5 semantic search (serverless, managed) | COMPLETE (optional) | Bedrock embeddings + DynamoDB; scale ceiling noted |
| **F6** all requested dashboard metrics | COMPLETE | `/stats` + console + CloudWatch |
| **F7** REQUIRES_REVIEW, display, correct, separate storage, audit trail, no silent overwrite | COMPLETE | |
| **F8** deadlines, expirations, renewals, events, actions, responsible parties in DynamoDB | COMPLETE | grounded flags |
| **F9** source document, page, snippet, relevance; explicit "not enough information" | COMPLETE | page null for v1-processed docs |
| **F10** JSON / CSV / PDF with all listed contents | COMPLETE | PDF limited to Windows-1252 glyphs |
| **F11** audit for all listed operations, no sensitive content | COMPLETE | best-effort writes, alarmed |
| Architecture: serverless/event-driven, loose coupling, stateless, idempotent, retries, DLQ | COMPLETE | |
| Least privilege, encryption, no hard-coded secrets, CloudWatch monitoring, IaC | COMPLETE | |
| Backward compatibility of existing APIs | COMPLETE | v1 contracts kept; v1-processed documents supported |
| Unit tests | COMPLETE | 61 offline tests |
| Integration tests | PARTIALLY COMPLETE | handler-level integration against fakes passes; live AWS test written but not executed here |
| README, API docs, architecture docs + diagram, samples | COMPLETE | |
| Deployable with AWS SAM | PARTIALLY COMPLETE | template checked by custom tests; `sam build` / `sam validate` / `cfn-lint` not run in this environment — run before first deploy |
| Frontend | COMPLETE | `frontend/index.html` (new; v1 had none) |
