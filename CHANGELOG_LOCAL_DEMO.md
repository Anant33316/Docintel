# Local demo changelog – October 2026

Scope: make the local demo (`python local_server.py` → http://127.0.0.1:8001/) reliable and honest, and
redesign the console. AWS deployment, real credentials and paid APIs are out of scope. Full technical detail is
in `PROJECT_ANALYSIS.md`.

## Assumptions

- The brief arrived truncated at section 7 ("All frontend API…"). I assumed the rest asked for:
  - all frontend API calls working through the local server on one origin,
  - unknown routes being handled cleanly,
  - configuration being documented,
  - tests and documentation being updated.
- "Do not migrate to the cloud": nothing in `template.yaml` changed. Production code changes are limited to
  three backward-compatible edits (listed below).
- Uploaded documents get **no** structured fields. Pulling `Label: value` pairs out of plain text would have
  looked like Textract extraction, which the brief asked me not to fake.

## Issues, root causes and changes

| # | Original issue | Root cause | Change |
|---|---|---|---|
| 1 | PDF upload failed (`ERR_NAME_NOT_RESOLVED`, failed preflight) | `FakeS3.generate_presigned_url` returned `https://local-bucket.s3.fake/...`; the local server also had no PUT route and nothing triggered processing | New `POST /local/upload` (same origin, raw bytes, validated, processed synchronously). `LocalS3` issues real `http://127.0.0.1:8001/local/s3/...` URLs, and `PUT /local/s3/{key}` makes the production `upload-url` + PUT flow work too |
| 2 | Arbitrary PDFs could not be processed | `FakeTextract` only returns blocks registered by the test helper `ingest()` | `LocalTextract`: PyMuPDF text → PAGE/LINE blocks only (no fake forms, tables or confidence), so the processor runs unchanged. Validation by content (`%PDF-` header), damage, password, size (20 MB), pages (300), empty file. A text-free PDF becomes a visible FAILED record explaining that OCR is needed |
| 3 | Canned "AI" output on every document ("An invoice from Acme Cloud.", invented "March 3, 2027" renewal, canned comparison, fake entity scores, fake token counts, hashed "semantic" search) | Local server used the test `Env` and its scripted `default_responder`/`FakeComprehend` | `LocalBedrock`/`LocalComprehend` use deterministic heuristics or report "unavailable". Embeddings off, token usage 0. Each document stores `provenance` (genuine / heuristic / fixture / unavailable for every stage), shown in the UI and in exports |
| 4 | Seeded records named `sample_invoice_v1/v2.pdf` didn't match the PDFs in `samples/` | They are the `INVOICE_A/B` test fixtures | Same keys, so the same IDs (`98e7e792…`, `ef241ea5…`, deterministic, tested). Renamed "Demo fixture - Acme invoice A/B" and labelled as fixtures |
| 5 | Broken characters across the UI (`â€“`, `Â·`, `ðŸŽ‰`) | `index.html` was re-saved on Windows: UTF-8 read as Windows-1252, plus a BOM and CRLF (the exact tool is a hypothesis) | New frontend files in clean UTF-8. `.gitattributes` and `.editorconfig` added; a test rejects BOM, CRLF and mojibake |
| 6 | Console needed an API URL even locally | `api()` threw when no URL was saved | Console detects the local server via `GET /local/config` and calls the same origin. API URL and key are only asked for in AWS mode |
| 7 | `/ui/index.html` returned 404 | No such route | `/ui`, `/ui/`, `/ui/index.html` and `/index.html` redirect to `/` |
| 8 | Export and redaction download links were dead | Same fake presigned host | `GET /local/s3/{exports,redacted}/...` with expiry enforced |
| 9 | Any website open in the browser could read or alter local data | `Access-Control-Allow-Origin: *`; POST routes accept `text/plain` JSON | CORS origin set to the local origin; non-GET requests from other origins rejected (403); unknown Host headers rejected (400) |
| 10 | `expectedVersion: "abc"` returned 500 | Unvalidated `int()` | 400 with a message (`src/api/review_api.py`) |
| 11 | Non-UTF-8 request body crashed the local gateway (500) | Unchecked `decode("utf-8")` | 400 |
| 12 | Frontend too basic | – | Full redesign (below) |

## Files

**New**

- `local_demo/__init__.py`, `pdf_extract.py`, `services.py`, `heuristics.py`, `pipeline.py`
- `frontend/assets/app.css`, `frontend/assets/app.js`
- `tests/test_local_demo.py` (24 tests)
- `requirements-local.txt`, `.gitattributes`, `.editorconfig`
- `PROJECT_ANALYSIS.md`, this file

**Rewritten**

- `local_server.py`
- `frontend/index.html`

**Modified (small, backward compatible)**

- `src/processor/app.py`: a missing or `None` entity score is stored as `null`.
- `src/export/app.py`, `src/export/formats.py`: when a document carries `provenance`, its labels replace the
  hard-coded "Amazon Bedrock / Comprehend" and "Comprehend score" text.
- `src/api/review_api.py`: `expectedVersion` is validated.
- `README.md`: "Run it locally" section; frontend hosting note.
- `docs/ARCHITECTURE.md`: frontend row.
- `.gitignore`: adds `.venv/`.

**Unchanged:** `template.yaml`, all other `src/` modules, all original tests, the sample PDFs and the scripts.

## Frontend redesign

- **Shell:** dark "ink" sidebar with a light workspace, a sticky top bar with breadcrumbs and Upload, and a
  drawer on phones. Bahnschrift headings and tabular figures, Segoe UI body (both ship with Windows, so it works
  offline). Inline SVG icons.
- **Pages:** Dashboard, Documents, Review queue, Search, Compare, Audit log and Settings, each backed by existing
  endpoints. There are no dead links.
- **Dashboard:**
  - status ledger (total, processed, need review, failed, plus a processing count) with a proportional status
    bar,
  - fixture versus uploaded counts,
  - recent documents and upcoming dates,
  - categories, labelled heuristic,
  - recent failures,
  - activity counts from the real audit counters.

  Every number comes from `/stats` or `/documents`.
- **Documents:** filter by name, status, review state and source; sortable columns; pagination; empty and
  no-match states; Open actions; stacked cards on phones.
- **Upload dialog:** drag-and-drop or file picker, the accepted format and size limit, file details, browser-side
  checks, an honest "uploading and processing" state with no fake percentage, success only after the server
  confirms, duplicate notice, and specific error titles with retry or choose-another options.
- **Document detail:** header with status, source, pages and ID, and a "View original PDF" link for uploads.
  Tabs: Overview, Fields & review, Extracted text, Ask, Privacy, Export, Audit. Each tab can be opened directly
  by URL and reached by keyboard.
- **Honesty:** each result carries a badge (Genuine, Heuristic, Demo fixture or Unavailable) with a tooltip
  explaining it, and the Overview has an expandable "How each result was produced" panel. Unavailable features
  say why (for example "Needs a language model"). Settings shows the full table of what runs locally.
- **Accessibility:** skip link, landmarks, `aria-current`, the ARIA tabs pattern, `aria-sort`, labelled inputs,
  live regions, visible focus, reduced-motion support, and colour never used as the only signal.

## Tests executed and actual outcomes

| Run | Result |
|---|---|
| Original suite before any change (Python 3.13 in this sandbox) | 61 passed, 1 skipped (live AWS) |
| Full suite after changes, no PyMuPDF | 79 passed, 7 skipped (6 PyMuPDF, 1 live AWS) |
| Full suite after changes, with a pypdf-based PyMuPDF stand-in (sandbox only, not shipped) | 85 passed, 1 skipped |
| Python 3.12 byte-compile of all changed Python files | OK |
| `node --check frontend/assets/app.js` | OK |
| Live server checks with curl | sample PDFs → 201; duplicate → 200 `duplicate:true`; text-free PDF → 422 `ocr_required` plus a FAILED record; non-PDF → 415; fixture IDs printed as before |
| Browser checks with Playwright + Chromium | Screenshots at 1440, 820 and 390 px. 15 scripted checks passed: upload result, image rejected in the browser, review correction (badge cleared, history updated), keyboard tabs, PDF export download, masked PII and redaction, status filter from the dashboard, `aria-sort`, `/ui/index.html` redirect, missing-document error, no JavaScript errors. AWS mode from `file://` routes to Settings with no errors |

**Not run here:** real PyMuPDF 1.28.2, because PyPI was blocked in this sandbox. Run `python -m pytest` on your
machine; the 6 PyMuPDF tests then execute against the real library.

## Remaining limitations

- No OCR: scanned or image PDFs and image files are refused with an explanation.
- No AI locally: intent, required actions, AI comparison, language detection, names and organisations are
  unavailable. Summary, category, dates, entities, key phrases, answers and PII detection are heuristics, and are
  labelled as such.
- Uploaded PDFs have no form fields, tables or confidence, so the review queue only shows the scripted fixture.
- Data is in memory and lost on restart. Processing is synchronous and serialised.
- The list API returns at most 50 documents (production behaviour, unchanged).
- The local server reuses `tests/fakes.py` and `tests/helpers.py`.

---

# Feature extension – October 2026 (second iteration)

Scope: five features from the extension brief, built into the existing pipeline. There are no new dependencies,
no AWS calls and no AI. Details are in `PROJECT_ANALYSIS.md` section 12.

## Stage 1: audit and baseline

The uploaded ZIP was byte-identical to the previous delivery. Before any change:

| Suite | Result |
|---|---|
| Without PyMuPDF | 79 passed, 7 skipped |
| With the sandbox PyMuPDF stand-in | 85 passed, 1 skipped |

PyPI is blocked in this sandbox, so real PyMuPDF could not be installed or run. The PDF tests run against a
pypdf-based stand-in, which is not shipped.

## Features added

**A. Structured invoice fields and validation** (`src/shared/docintel_common/invoice.py`)

- Eleven fields, each with its value as written, normalized value, page, source line and status: extracted,
  inferred, ambiguous, invalid, missing or corrected.
- Date and money normalization covers several formats, separators and currencies. Ambiguous cases are flagged,
  not guessed.
- Checks: arithmetic (subtotal + tax + shipping − discount, within 0.01), tax rate, date order and negative
  amounts. A difference that no tax or discount line explains is a warning, not an error.
- **Review integration:** when OCR found no key-value pairs (local uploads), the parsed fields become review
  fields with no confidence score. Corrections are re-parsed, and the checks use them.
- OCR key-value pairs are also fed to the parser.

**B. Evidence-backed answers** (`local_demo/heuristics.answer`, `src/api/qa.py`)

- Three answer types: a template answer from a retrieved field, citing the verbatim document line; an extractive
  best line (at least 50% of the question's terms); or "not found".
- Every answer carries `answerMode` (`template` / `extractive` / `none` / `llm`), shown as a badge in Ask.
- Citations are still verified by the unchanged `grounding.interpret`.
- Search snippets are no longer repeated.

**C. Explainable anomaly detection** (`anomalies.py`, `src/api/invoices.py`)

- Twelve rules with fixed severities, covering:
  - missing, ambiguous or unreadable fields,
  - arithmetic and tax mismatches,
  - due date before invoice date, negative amounts,
  - high value (configurable),
  - duplicate invoice numbers and possible duplicate submissions,
  - value changes between linked versions.
- Each finding shows its rule, evidence (value, page, source line), explanation and suggested review.
- Acknowledge, dismiss (a note is required) or reopen. Decisions are stored, and audited as `ANOMALY_REVIEWED`
  without the note text.
- New Anomalies page and dashboard panel.

**D. Processing-stage diagnostics** (`src/processor/app.py`, `local_demo/pipeline.py`)

- Seven stages, with measured durations, safe error messages and a retry hint.
- `POST /local/documents/{id}/retry` re-runs the stored file. It reuses the same record and is refused when a
  retry cannot help (e.g. OCR needed).
- Repeat attempts are audited as `PROCESSING_RETRIED` instead of a second `DOCUMENT_UPLOADED`. This also fixes
  duplicate audit entries on SQS redelivery in AWS.

**E. Document version history** (`src/api/versions.py`)

- Link a document as a revision (from the Versions tab, or while uploading).
- Timeline view, field differences (added, removed, changed) and a difflib text diff, labelled as detected
  differences, not interpretations.
- "Full comparison" opens Compare with both documents preselected.
- Linked versions are excluded from the duplicate rules.

## Files changed

**New**

- `src/shared/docintel_common/invoice.py`, `src/shared/docintel_common/anomalies.py`
- `src/api/invoices.py`, `src/api/versions.py`
- `tests/test_invoice.py`, `tests/test_anomalies.py`, `tests/test_versions.py`

**Modified**

- `src/processor/app.py`: invoice parsing, stage diagnostics, attempt/etag on claim, retry-aware audit, a pending
  status for unscored fields.
- `src/api/app.py`: five routes; version keys in the list response.
- `src/api/qa.py`: `answerMode`.
- `src/api/search.py`: snippet de-duplication.
- `src/export/app.py`: invoice fields in the JSON export.
- `src/shared/docintel_common/review.py`: field `source`, `parseStatus` and `evidence`.
- `src/shared/docintel_common/audit.py`: three event types.
- `template.yaml`: five API events and `HIGH_VALUE_THRESHOLD`.
- `local_server.py`: retry route.
- `local_demo/pipeline.py`: stages, retry, new provenance entries.
- `local_demo/services.py`, `local_demo/heuristics.py`: answer engine.
- `local_demo/pdf_extract.py`: `public` and `retryable` flags.
- `frontend/index.html`, `frontend/assets/app.js`, `frontend/assets/app.css`: Anomalies page, Invoice checks and
  Versions tabs, stages panel, answer badges, upload revision option, dashboard panel.
- `tests/test_local_demo.py`: +10 tests; one assertion updated because uploads now have parser fields.
- `README.md`, `PROJECT_ANALYSIS.md`, this file.

## Important decisions

- **Parser fields join the review queue only when OCR produced no fields.** Documents that have Textract fields
  keep them as the review fields, and their parsed invoice values are shown in Invoice checks.
- **Findings are computed on request, not stored.** Only reviewers' decisions are persisted, so a correction
  changes the findings immediately. Finding ids hash the evidence, so a decision cannot silently carry over to
  different values.
- **Severity comes from a fixed, documented table.** There is no score or model, and the wording avoids any
  claim of fraud.
- **The arithmetic check includes only charges it can recognise** (tax, shipping, discount). It reports a mismatch
  together with that limitation.
- **New API routes are production routes, wired in `template.yaml`.** The only local-only addition is retry, since
  AWS retries through SQS.

## Tests executed (actual results, this sandbox)

| Run | Result |
|---|---|
| Full suite, no PyMuPDF | 139 passed, 12 skipped (11 PyMuPDF, 1 live AWS) |
| Full suite, with the PyMuPDF stand-in | **150 passed, 1 skipped** |
| Python 3.12 byte-compile of all changed Python | OK |
| `node --check frontend/assets/app.js` | OK |
| Browser (Playwright + Chromium) | 15 regression checks (1 updated for the new review behaviour) and 14 new-feature checks, all passing, with no JavaScript errors. Screenshots at 1440, 1366, 820 and 390 px |

By kind:

- 68 unit tests,
- 82 integration tests against local substitutes,
- 1 live-AWS test (skipped).

**Not run here:** real PyMuPDF 1.28.2 and a real AWS deployment.

## Known limitations

- The invoice parser reads labelled values from text lines. Labels and values on separate lines (common in
  table layouts), line items and scanned documents are not handled; they show up as "not found" or "ambiguous".
- The vendor is inferred from the heading when it has no label, and is marked inferred. Currency "$" is assumed
  to be USD (noted).
- The high-value threshold is a single number and does no currency conversion.
- The anomaly list scans every document on each request. That is fine for a demo; it would need indexing at scale.
- Versions can only be appended (no unlinking, no reordering).
- Local answers are template or extractive only; questions that need reasoning are answered "not found".
- Invoice fields appear in the JSON export only, not in CSV or PDF.
- All the earlier limitations still apply: no OCR, no AI, in-memory storage, a 50-document list limit.

## Incomplete requirements

- **Cross-document question answering** (one question over all documents) was not added. Search covers
  cross-document evidence retrieval, and per-document Ask is evidence-backed. This is listed as a follow-up.
- **Line-level arithmetic** (quantity × price) was not implemented; it needs table extraction.
