"""Render the export model as JSON, CSV or a human-readable PDF report."""
import csv
import io
import json
from datetime import datetime, timezone

from pdf_report import PdfReport

MAX_PDF_TEXT_CHARS = 60000


def to_json(model):
    return json.dumps(model, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def _safe_cell(v):
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a cell)."""
    if v is None:
        return ""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(model):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "item", "key", "value", "confidence", "status", "page", "notes"])

    def row(section, item="", key="", value="", confidence="", status="", page="", notes=""):
        w.writerow([_safe_cell(x) for x in (section, item, key, value, confidence, status, page, notes)])

    for k, v in model["document"].items():
        row("metadata", key=k, value=v)
    row("classification", key="category", value=model["classification"]["category"],
        notes=model["classification"]["method"])
    row("summary", value=model["aiInsights"]["summary"])
    row("intent", value=model["aiInsights"]["intent"])
    for i, p in enumerate(model["aiInsights"]["keyPhrases"], 1):
        row("keyPhrase", item=i, value=p)
    for i, e in enumerate(model["entities"], 1):
        row("entity", item=i, key=e.get("type"), value=e.get("text"), confidence=e.get("score"),
            notes="Comprehend score 0-1" if e.get("score") is not None else "no score")
    for i, d in enumerate(model["keyDates"], 1):
        row("keyDate", item=i, key=d.get("type"), value=d.get("isoDate") or d.get("date"),
            status="grounded" if d.get("grounded") else "unverified", notes=d.get("description"))
    for i, a in enumerate(model["actions"], 1):
        row("action", item=i, key=a.get("responsibleParty"), value=a.get("action"),
            status="grounded" if a.get("grounded") else "unverified",
            notes=f"deadline: {a.get('isoDeadline') or a.get('deadline') or '-'}")
    for f in model["fields"]:
        row("field", item=f["fieldId"], key=f["key"], value=f["effectiveValue"], confidence=f["confidence"],
            status=f["status"], page=f["page"],
            notes=f"AI value: {f['aiValue']}" if f["status"] == "CORRECTED" else "")
    for t, table in enumerate(model["tables"], 1):
        for r, cells in enumerate(table, 1):
            row("table", item=f"{t}.{r}", value=" | ".join(cells))
    if model.get("extractedText") is not None:
        for i, line in enumerate(model["extractedText"].split("\n"), 1):
            row("text", item=i, value=line)
    return buf.getvalue().encode("utf-8-sig")      # BOM so Excel opens UTF-8 correctly


def _fmt_conf(c):
    return f"{c:.1f}%" if isinstance(c, (int, float)) else "n/a"


def to_pdf(model):
    d = model["document"]
    pdf = PdfReport(f"DocIntel report - {d.get('fileName') or d['docId']}")
    pdf.heading("Document Intelligence Report", level=0)
    pdf.text(f"Generated {model['generatedAt']} by {model['generatedBy']}", size=9, gray=0.4)
    pdf.space(8)

    pdf.heading("Document")
    for k in ("fileName", "docId", "status", "language", "pageCount", "createdAt", "completedAt",
              "processingSeconds", "avgOcrConfidence"):
        v = d.get(k)
        if k in ("createdAt", "completedAt") and isinstance(v, (int, float)):
            v = datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pdf.kv(k, v if v is not None else "-")

    pdf.heading("Classification & summary")
    pdf.kv("Category", f"{model['classification']['category']} ({model['classification']['method']})")
    pdf.text(model["aiInsights"]["summary"] or "-")
    pdf.space()
    pdf.kv("Intent", model["aiInsights"]["intent"] or "-")
    if model["aiInsights"]["keyPhrases"]:
        pdf.kv("Key phrases", ", ".join(model["aiInsights"]["keyPhrases"]))
    if model.get("provenance"):
        pdf.text(model["aiInsights"]["generatedBy"], size=8, gray=0.45)
    else:
        pdf.text("Summary, intent and category are AI-generated (Amazon Bedrock).", size=8, gray=0.45)

    pdf.heading("Key dates & required actions")
    if not model["keyDates"] and not model["actions"]:
        pdf.text("None found.")
    for kd in model["keyDates"]:
        flag = "" if kd.get("grounded") else "  [unverified]"
        pdf.bullet(f"{(kd.get('type') or '').upper()}: {kd.get('isoDate') or kd.get('date')} - "
                   f"{kd.get('description') or ''}{flag}")
    for a in model["actions"]:
        flag = "" if a.get("grounded") else "  [unverified]"
        who = f" (responsible: {a['responsibleParty']})" if a.get("responsibleParty") else ""
        due = f" - due {a.get('isoDeadline') or a.get('deadline')}" if (a.get("isoDeadline") or a.get("deadline")) else ""
        pdf.bullet(f"ACTION: {a.get('action')}{due}{who}{flag}")

    conf = model["confidence"]
    pdf.heading("Extracted fields & confidence")
    s = conf["summary"]
    pdf.text(f"Threshold {conf['threshold']}%: {s['totalFields']} fields, {s['lowConfidence']} below threshold, "
             f"{s['pendingReview']} awaiting review, {s['verified']} verified, {s['corrected']} corrected.", size=9)
    pdf.space(4)
    if not model["fields"]:
        pdf.text("No form fields were extracted.")
    for f in model["fields"]:
        line = f"{f['key']}: {f['effectiveValue']}   [{_fmt_conf(f['confidence'])}, {f['status']}"
        line += f", p.{f['page']}]" if f.get("page") else "]"
        if f["status"] == "CORRECTED":
            line += f"   (AI value: {f['aiValue']})"
        pdf.bullet(line, size=9)

    if model["entities"]:
        pdf.heading("Important entities")
        for e in model["entities"]:
            score = f"{e['score']:.2f}" if isinstance(e.get("score"), (int, float)) else "n/a"
            pdf.bullet(f"{e.get('type')}: {e.get('text')} (score {score})", size=9)

    if model["tables"]:
        pdf.heading("Tables")
        for t, table in enumerate(model["tables"], 1):
            pdf.text(f"Table {t}", bold=True, size=9)
            for cells in table[:60]:
                pdf.text(" | ".join(cells), size=8, indent=10)
            pdf.space(4)

    if model.get("piiSummary"):
        pdf.heading("Privacy scan")
        p = model["piiSummary"]
        pdf.text(f"Last scan {p.get('scannedAt')}: {p.get('total', 0)} PII entities "
                 f"({', '.join(f'{k}: {v}' for k, v in (p.get('counts') or {}).items()) or 'none'}).")

    if model.get("extractedText") is not None:
        pdf.heading("Extracted text")
        text = model["extractedText"]
        if len(text) > MAX_PDF_TEXT_CHARS:
            text = text[:MAX_PDF_TEXT_CHARS] + "\n[... truncated in PDF; see the JSON or CSV export for the full text]"
        pdf.text(text, size=8)
    return pdf.build()
