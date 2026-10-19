# DocIntel API reference (v2)

Base URL: the `ApiUrl` stack output, e.g. `https://abc123.execute-api.us-east-1.amazonaws.com/prod`.

**Headers**

| Header | Required | Meaning |
|---|---|---|
| `x-api-key` | yes | API key from the usage plan (`ApiKeyId` output) |
| `Content-Type: application/json` | for bodies | |
| `X-User-Id` | no | Caller label used for chat-session ownership and the audit trail (default `anonymous`). Not an authenticated identity — see limitations. |

**Errors** are always `{"error": "<message>"}` with `400` (validation), `403` (chat session owned by someone else), `404` (not found), `409` (document not `COMPLETED` / concurrent review conflict), `500` (unexpected, details only in CloudWatch). API Gateway itself returns `403 {"message":"Forbidden"}` for a missing/invalid key and `429` when throttled.

**Conventions**: confidence from Textract is **0–100**; Comprehend scores are **0–1**; retrieval `relevance` is **0–1** relative to the best passage for that question. Timestamps are epoch seconds (document lifecycle) or ISO-8601 UTC strings (everything new in v2). The sample values below were produced by running the real handlers against the test fixtures (IDs shortened).

```bash
export API=https://abc123.execute-api.us-east-1.amazonaws.com/prod KEY=xxxxxxxx
```

---

## Existing endpoints (unchanged contracts)

### POST `/documents/upload-url`
```bash
curl -s -X POST "$API/documents/upload-url" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"fileName":"invoice.pdf"}'
```
```json
{"uploadUrl": "https://...s3...", "key": "uploads/5f1c.../invoice.pdf", "contentType": "application/pdf",
 "note": "PUT the file to uploadUrl with the same Content-Type header"}
```
Then `curl -X PUT -H "Content-Type: application/pdf" --data-binary @invoice.pdf "<uploadUrl>"`.

### GET `/documents`
Recent documents. v2 adds `completedAt`, `reviewStatus`, `pageCount` to each row.
```json
{"documents": [{"docId": "7765a40a…", "fileName": "invoice-a.png", "status": "COMPLETED", "category": "invoice",
  "createdAt": 1791619681, "completedAt": 1791619682, "reviewStatus": "PENDING_REVIEW", "pageCount": 2}]}
```

### GET `/documents/{docId}`
Everything stored for the document. New v2 attributes (all additive):

| Attribute | Description |
|---|---|
| `fields[]` | `{fieldId, key, value, keyConfidence, valueConfidence, confidence, page, reviewStatus}` — original AI extraction, never modified |
| `verifications{}` | human decisions per `fieldId` (see verify) |
| `confidenceThreshold`, `reviewStatus`, `reviewSummary` | `PENDING_REVIEW` / `NOT_REQUIRED` / `REVIEWED` |
| `entities[]` | now include `score` (Comprehend, 0–1) |
| `keyDates[]` | `{type, date, isoDate, description, sourceText, grounded}` |
| `actions[]` | `{action, deadline, isoDeadline, responsibleParty, sourceText, grounded}` |
| `processingMs`, `aiStats`, `avgOcrConfidence`, `chunkCount`, `classificationMethod`, `piiSummary` | processing metadata |

```json
{
  "docId": "7765a40a…", "fileName": "invoice-a.png", "status": "COMPLETED", "category": "invoice",
  "summary": "An invoice from Acme Cloud.", "intent": "Request payment.", "language": "en",
  "fields": [{"fieldId": "f002", "key": "Total", "value": "40 USD", "keyConfidence": 95, "valueConfidence": 62,
              "confidence": 62, "page": 1, "reviewStatus": "REQUIRES_REVIEW"}],
  "keyDates": [{"type": "deadline", "date": "October 20, 2026", "isoDate": "2026-10-20",
                "description": "Report submission deadline",
                "sourceText": "Submit the report before October 20, 2026.", "grounded": true}],
  "actions": [{"action": "Submit report", "deadline": "October 20, 2026", "isoDeadline": "2026-10-20",
               "responsibleParty": "Finance team", "sourceText": "Submit the report before October 20, 2026.",
               "grounded": true}],
  "reviewStatus": "PENDING_REVIEW", "reviewSummary": {"threshold": 90, "totalFields": 3, "lowConfidence": 1},
  "processingMs": 8421, "avgOcrConfidence": 98.23,
  "aiStats": {"bedrockCalls": 2, "bedrockInputTokens": 3120, "bedrockOutputTokens": 410, "embeddingCalls": 8,
              "comprehendCalls": 3, "textractPages": 2}
}
```

### POST `/documents/{docId}/ask`  — upgraded (Feature 9)
Same request and the same `question`/`answer` keys as v1; the answer is now built **only from retrieved passages** and comes with sources.
```bash
curl -s -X POST "$API/documents/$DOC/ask" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"question":"What is the total amount due?"}'
```
```json
{
  "question": "What is the total amount due?",
  "docId": "7765a40a…",
  "answer": "Total amount due: 40 USD [S1]",
  "answerable": true,
  "confidence": "high",
  "modelConfidence": "high",
  "citations": [{"sourceId": "S1", "docId": "7765a40a…", "fileName": "invoice-a.png", "page": 1,
                 "chunkId": "c0001", "snippet": "Total amount due: 40 USD", "relevance": 0.8309, "ocrConfidence": 98.87}],
  "quotes": [{"source": "S1", "quote": "Total amount due: 40 USD", "verified": true}],
  "warnings": [],
  "retrieval": {"mode": "hybrid", "sources": [
    {"id": "S1", "page": 1, "kind": "text", "relevance": 0.8309, "lexicalScore": 1.0, "semanticScore": 0.5774,
     "ocrConfidence": 98.87, "chunkId": "c0001"},
    {"id": "S2", "page": 1, "kind": "field", "relevance": 0.3391, "lexicalScore": 0.3429, "semanticScore": 0.3333,
     "ocrConfidence": 62, "chunkId": "kf002"}]}
}
```
* `answerable: false` → `answer` starts with **"The document does not contain enough information to answer this question."**, `citations` is empty and `confidence` is `"none"`.
* `confidence` is the model's self-report **lowered** by our checks: no valid citation → `low`; quotes not found verbatim → `low`; no verified quote → `high` is capped at `medium`.
* `retrieval.mode`: `hybrid` (BM25 + embeddings), `lexical` (embeddings disabled), or `fallback-first-pages` (nothing matched; the first pages were supplied).

---

## Feature 2 — Chat with history

### POST `/documents/{docId}/chat`
Body: `{"question": "...", "sessionId": "<optional>"}`. Omit `sessionId` to start a conversation; reuse the returned one for follow-ups. The last 3 exchanges are sent to the model; retrieval uses the current + previous question so follow-ups work. Response = the `/ask` response + `sessionId`.
```bash
curl -s -X POST "$API/documents/$DOC/chat" -H "x-api-key: $KEY" -H "X-User-Id: alice" \
  -H "Content-Type: application/json" -d '{"question":"Who won the football match?","sessionId":"2cba8e3c…"}'
```
```json
{"docId": "7765a40a…", "sessionId": "2cba8e3c87264c5e8059acb2127cd9fe", "question": "Who won the football match?",
 "answer": "The document does not contain enough information to answer this question.",
 "answerable": false, "confidence": "none", "modelConfidence": "low", "citations": [], "quotes": [], "warnings": [],
 "retrieval": {"mode": "hybrid", "sources": ["…"]}}
```
Errors: `404` unknown session, `403` session belongs to another `X-User-Id`.

### GET `/documents/{docId}/chat`
```json
{"docId": "7765a40a…", "userId": "alice",
 "sessions": [{"sessionId": "2cba8e3c…", "title": "What is the total amount due?",
               "createdAt": "2026-10-10T08:08:01.160Z", "updatedAt": "2026-10-10T08:08:01.163Z", "messageCount": 4}]}
```

### GET `/documents/{docId}/chat/{sessionId}`
```json
{"docId": "7765a40a…", "sessionId": "2cba8e3c…", "title": "What is the total amount due?",
 "messages": [
   {"role": "user", "text": "What is the total amount due?", "createdAt": "2026-10-10T08:08:01.160Z"},
   {"role": "assistant", "text": "Total amount due: 40 USD [S1]", "answerable": true, "confidence": "high",
    "citations": [{"sourceId": "S1", "page": 1, "snippet": "Total amount due: 40 USD", "relevance": 0.83}],
    "warnings": [], "createdAt": "2026-10-10T08:08:01.160Z"}]}
```

### DELETE `/documents/{docId}/chat/{sessionId}`
```json
{"deleted": true, "sessionId": "2cba8e3c…", "messagesDeleted": 4}
```
Messages also expire automatically after `ChatHistoryTtlDays` (DynamoDB TTL).

---

## Features 3 + 7 — Confidence and human verification

### GET `/documents/{docId}/fields`
Query: `threshold` (0–100, default = stack `ConfidenceThreshold`), `status` (`REQUIRES_REVIEW`, `AUTO_ACCEPTED`, `VERIFIED`, `CORRECTED`).
```bash
curl -s "$API/documents/$DOC/fields?status=REQUIRES_REVIEW" -H "x-api-key: $KEY"
```
```json
{
  "docId": "7765a40a…",
  "confidenceSource": "Amazon Textract (min of key and value block confidence, 0-100)",
  "summary": {"threshold": 90.0, "totalFields": 3, "lowConfidence": 1, "pendingReview": 1, "verified": 0,
              "corrected": 0, "reviewStatus": "PENDING_REVIEW"},
  "fields": [{"fieldId": "f002", "key": "Total", "page": 1, "aiValue": "40 USD", "confidence": 62,
              "keyConfidence": 95, "valueConfidence": 62, "requiresVerification": true,
              "aiStatus": "REQUIRES_REVIEW", "status": "REQUIRES_REVIEW", "correctedValue": null,
              "effectiveValue": "40 USD", "verifiedAt": null, "verifiedBy": null, "version": 0}],
  "entities": [{"type": "PERSON", "text": "John Smith", "score": 0.91}],
  "avgOcrConfidence": 98.23
}
```

### POST `/documents/{docId}/fields/{fieldId}/verify`
Body: `{"action": "confirm"}` or `{"action": "correct", "correctedValue": "48 USD", "note": "optional", "expectedVersion": 0}`.
`expectedVersion` (optional) enables optimistic locking → `409` if someone else changed the field.
```bash
curl -s -X POST "$API/documents/$DOC/fields/f002/verify" -H "x-api-key: $KEY" -H "X-User-Id: reviewer1" \
  -H "Content-Type: application/json" -d '{"action":"correct","correctedValue":"48 USD","note":"checked against the paper copy"}'
```
```json
{
  "docId": "7765a40a…",
  "field": {"fieldId": "f002", "key": "Total", "page": 1, "aiValue": "40 USD", "confidence": 62,
            "keyConfidence": 95, "valueConfidence": 62, "requiresVerification": false,
            "aiStatus": "REQUIRES_REVIEW", "status": "CORRECTED", "correctedValue": "48 USD",
            "effectiveValue": "48 USD", "verifiedAt": "2026-10-10T08:08:01.166Z", "verifiedBy": "reviewer1", "version": 1},
  "reviewSummary": {"threshold": 90.0, "totalFields": 3, "lowConfidence": 1, "pendingReview": 0, "verified": 0,
                    "corrected": 1, "reviewStatus": "REVIEWED"}
}
```
`aiValue` is never changed. "Correcting" to the AI value is recorded as `VERIFIED`.

### GET `/documents/{docId}/verifications`  (optional `?fieldId=`)
```json
{"docId": "7765a40a…", "auditTrail": [
  {"docId": "7765a40a…", "fieldId": "f002", "fieldKey": "Total", "aiConfidence": 62,
   "originalValue": "40 USD", "previousCorrectedValue": null, "previousStatus": "REQUIRES_REVIEW",
   "correctedValue": "48 USD", "status": "CORRECTED", "verifiedAt": "2026-10-10T08:08:01.166Z",
   "verifiedBy": "reviewer1", "note": "checked against the paper copy", "version": 1}]}
```

---

## Feature 1 — Comparison

### POST `/compare`
Body: `{"docIdA": "...", "docIdB": "...", "force": false}`. Both must be `COMPLETED`. "Added" means present in B but not A. Results are cached; `force: true` recomputes.
```bash
curl -s -X POST "$API/compare" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d "{\"docIdA\":\"$DOC_A\",\"docIdB\":\"$DOC_B\"}"
```
```json
{
  "comparisonId": "65e9232b7738c217e851d7ce", "docIdA": "7765a40a…", "docIdB": "096e9321…",
  "fileNameA": "invoice-a.png", "fileNameB": "invoice-b.pdf", "createdAt": "2026-10-10T08:08:01.169Z",
  "requestedBy": "anonymous", "truncated": false, "cached": false,
  "summary": "B raises the total and adds a late fee.",
  "factualDiff": {
    "method": "deterministic",
    "description": "Exact line-level and extracted-field differences computed with difflib; no AI involved.",
    "text": {"lineCountA": 7, "lineCountB": 4, "unchangedLines": 2, "addedCount": 1, "removedCount": 4, "changedCount": 1,
             "added": ["Late fee: 5 USD applies after the due date"],
             "removed": ["Bill to: John Smith, 12 Baker Street", "…"],
             "changed": [{"before": "Total amount due: 40 USD", "after": "Total amount due: 55 USD", "similarity": 0.917}],
             "lineSimilarity": 0.3636, "vocabularyOverlap": 0.2927, "truncated": false},
    "fields": {"added": [], "removed": [{"key": "Account Number", "value": "12345678"}],
               "changed": [{"key": "Invoice Number", "before": "INV-001", "after": "INV-002"},
                           {"key": "Total", "before": "40 USD", "after": "55 USD"}], "unchanged": []}
  },
  "aiInterpretation": {
    "status": "OK", "generatedBy": "LLM", "model": "anthropic.claude-3-haiku-20240307-v1:0",
    "disclaimer": "AI-generated interpretation. Items with verified=false could not be matched verbatim to the documents and must be checked manually.",
    "summary": "B raises the total and adds a late fee.",
    "added": [{"point": "Late fee introduced", "evidenceB": "Late fee: 5 USD applies after the due date", "verified": true}],
    "removed": [],
    "changed": [{"point": "Total changed", "before": "Total amount due: 40 USD", "after": "Total amount due: 55 USD", "verified": true}],
    "contradictions": [{"point": "Due dates conflict", "docA": "Due in 30 days", "docB": "Due immediately", "verified": false}],
    "similarities": [{"point": "Same vendor", "evidenceA": "Acme Cloud Services Invoice", "evidenceB": "Acme Cloud Services Invoice", "verified": true}]
  }
}
```
If Bedrock fails, `aiInterpretation.status` is `FAILED` (or `UNPARSEABLE`) and the factual diff is still returned.

### GET `/comparisons/{comparisonId}` → the stored object above.
### GET `/documents/{docId}/comparisons`
```json
{"docId": "7765a40a…", "comparisons": [{"comparisonId": "65e9232b…", "docIdA": "7765a40a…", "docIdB": "096e9321…",
  "createdAt": "2026-10-10T08:08:01.169Z"}]}
```

---

## Feature 5 — Search

### GET `/search`
| Query param | Meaning |
|---|---|
| `q` | text (max 300 chars). Omit for filter-only browsing (newest first) |
| `category` | e.g. `invoice` |
| `status` | `COMPLETED`, `FAILED`, `PROCESSING` |
| `dateFrom`, `dateTo` | `YYYY-MM-DD` (upload date, UTC, inclusive) |
| `limit` | 1–50 (default 10) |
| `semantic` | `false` to force lexical-only ranking |

```bash
curl -s "$API/search?q=AWS%20Lambda&status=COMPLETED" -H "x-api-key: $KEY"
```
```json
{
  "query": "AWS Lambda", "searchMode": "hybrid", "total": 1, "scannedChunks": 8, "indexTruncated": false,
  "filters": {"category": null, "status": "COMPLETED", "dateFrom": null, "dateTo": null},
  "results": [{"docId": "7765a40a…", "fileName": "invoice-a.png", "category": "invoice", "status": "COMPLETED",
               "createdAt": 1791619681, "score": 0.8798, "matchedTerms": ["aws", "lambda"], "matchCount": 1,
               "snippets": [{"text": "… Submit the report before October 20, 2026. Workloads run on AWS Lambda.",
                             "page": 2, "kind": "text", "score": 0.8798, "semanticScore": 0.3244}]}]
}
```
Ranking: BM25 over all chunks + per-document metadata (file name, category, summary, key phrases, entities), blended 60/40 with embedding cosine similarity when available, +0.15 for an exact phrase match, small boost for multiple matching passages.

---

## Feature 4 — PII (Privacy function)

### POST `/documents/{docId}/pii`  (detection mode)
Body (all optional): `{"minScore": 0.5, "entityTypes": ["NAME", "EMAIL"]}`. Types are Amazon Comprehend PII types (`NAME`, `EMAIL`, `PHONE`, `ADDRESS`, `SSN`, `BANK_ACCOUNT_NUMBER`, `CREDIT_DEBIT_NUMBER`, `PASSPORT_NUMBER`, `DRIVER_ID`, `IN_AADHAAR`, `IN_PERMANENT_ACCOUNT_NUMBER`, …). Returns **masked previews only**; raw values are never returned or stored.
```json
{
  "docId": "7765a40a…", "mode": "detect", "scannedAt": "2026-10-10T08:08:01.175Z", "languageCode": "en",
  "minScore": 0.5, "total": 4, "counts": {"NAME": 1, "ADDRESS": 1, "EMAIL": 1, "BANK_ACCOUNT_NUMBER": 1},
  "entities": [
    {"type": "NAME", "score": 0.99, "location": "line", "page": 2, "ref": 0, "preview": "J*** S****"},
    {"type": "BANK_ACCOUNT_NUMBER", "score": 0.97, "location": "fieldValue", "page": 2, "ref": "f003", "preview": "1*******"}],
  "warning": null
}
```
Comprehend PII supports English and Spanish; other languages are scanned as English with a `warning`.

### POST `/documents/{docId}/redact`
Body (all optional): `{"entityTypes": [...], "minScore": 0.5, "mask": "TYPE" | "CHAR"}` (`TYPE` → `[NAME]`, `CHAR` → `*****`).
```json
{
  "docId": "7765a40a…", "redactionId": "04359ace42b74bf2", "createdAt": "2026-10-10T08:08:01.175Z",
  "requestedBy": "anonymous", "entityTypes": ["NAME", "EMAIL"], "minScore": 0.5, "mask": "TYPE", "languageCode": "en",
  "counts": {"NAME": 1, "EMAIL": 1}, "totalRedacted": 2,
  "sourceResultKey": "results/7765a40a….json", "sha256": "a38505f2…",
  "objects": {"json": "redacted/7765a40a…/04359ace42b74bf2/redacted.json",
              "text": "redacted/7765a40a…/04359ace42b74bf2/redacted.txt"},
  "downloads": {"json": "https://…presigned…", "text": "https://…presigned…"}, "urlExpiresInSeconds": 300
}
```

### GET `/documents/{docId}/redactions` → `{"docId", "redactions": [<metadata as above with fresh downloads>]}`

---

## Feature 10 — Export (Export function)

### GET `/documents/{docId}/export?format=json|csv|pdf&includeText=true`
```bash
curl -s "$API/documents/$DOC/export?format=pdf" -H "x-api-key: $KEY"
```
```json
{"docId": "7765a40a…", "exportId": "a7f81eb9fee240c1", "format": "pdf", "bytes": 2773,
 "objectKey": "exports/7765a40a…/a7f81eb9fee240c1.pdf", "downloadUrl": "https://…presigned…", "urlExpiresInSeconds": 300}
```
* **JSON**: `document`, `classification`, `aiInsights` (summary/intent/key phrases), `entities`, `keyDates`, `actions`, `confidence` (threshold + summary), `fields` (AI value, confidence, status, corrected and effective values), `tables`, `piiSummary`, `aiStats`, `extractedText`.
* **CSV**: one row per item: `section,item,key,value,confidence,status,page,notes` (UTF-8 with BOM; cells starting with `= + - @` are prefixed with `'`).
* **PDF**: human-readable report (document, classification & summary, key dates & actions, fields with confidence and corrections, entities, tables, privacy scan, extracted text up to 60k chars).
Export files are deleted automatically after 7 days.

---

## Feature 6 — Dashboard

### GET `/stats`
```json
{
  "generatedAt": "2026-10-10T08:08:01+00:00", "sampled": false,
  "documents": {"total": 2, "completed": 2, "failed": 0, "processing": 0, "byStatus": {"COMPLETED": 2}},
  "categories": {"invoice": 2},
  "processingTimeSeconds": {"average": 8.4, "p50": 7.9, "p90": 11.2, "max": 11.2, "samples": 2},
  "failures": {"total": 0, "recent": []},
  "recentUploads": [{"docId": "096e9321…", "fileName": "invoice-b.pdf", "status": "COMPLETED", "category": "invoice", "createdAt": 1791619681}],
  "uploadsPerDay": [{"date": "2026-09-27", "count": 0}, "…", {"date": "2026-10-10", "count": 2}],
  "ai": {"documentsAnalyzed": 2, "textractPages": 3, "comprehendCalls": 6, "embeddingCalls": 8,
         "bedrockCallsDuringProcessing": 4, "bedrockInputTokens": 800, "bedrockOutputTokens": 160,
         "questionsAnswered": 3, "comparisons": 1, "searches": 1, "piiScans": 1, "redactions": 1, "exports": 1,
         "fieldVerifications": 1, "averageOcrConfidence": 98.31},
  "review": {"documentsPendingReview": 0, "lowConfidenceFields": 1},
  "upcomingKeyDates": [{"docId": "7765a40a…", "fileName": "invoice-a.png", "type": "deadline", "date": "2026-10-20",
                        "description": "Report submission deadline"}],
  "eventCounts": {"DOCUMENT_UPLOADED": 2, "DOCUMENT_PROCESSED": 2, "DOCUMENT_QUERIED": 3, "…": 1}
}
```

---

## Feature 11 — Audit trail

### GET `/documents/{docId}/audit` (newest first)
```json
{"docId": "7765a40a…", "events": [
  {"eventId": "9b1c…", "eventType": "EXPORT_GENERATED", "docId": "7765a40a…", "actor": "alice",
   "timestamp": "2026-10-10T08:08:01.177Z", "requestId": "c2f1…",
   "details": {"format": "pdf", "bytes": 2773, "exportId": "a7f81eb9fee240c1", "includeText": true}},
  {"eventId": "41aa…", "eventType": "DOCUMENT_PROCESSED", "docId": "7765a40a…", "actor": "system",
   "timestamp": "2026-10-10T08:07:59.010Z",
   "details": {"category": "invoice", "pages": 2, "fields": 3, "lowConfidenceFields": 1, "keyDates": 2, "processingMs": 8421}}]}
```

### GET `/audit?date=YYYY-MM-DD&eventType=...` → all events of one UTC day (default today).

Event types: `UPLOAD_URL_ISSUED`, `DOCUMENT_UPLOADED`, `DOCUMENT_PROCESSED`, `DOCUMENT_FAILED`, `DOCUMENT_QUERIED`, `DOCUMENT_COMPARED`, `DOCUMENTS_SEARCHED`, `PII_DETECTED`, `PII_REDACTED`, `RESULT_MODIFIED`, `FIELD_VERIFIED`, `EXPORT_GENERATED`, `CHAT_SESSION_DELETED`. `details` never contains document text, questions, answers, field values or search terms.
