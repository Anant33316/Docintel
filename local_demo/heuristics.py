"""Deterministic, offline text heuristics used by the local demo in place of AI services.

Everything here is plain pattern matching / counting over the document's own text. It is
*genuine local computation* (the output really is derived from the uploaded document), but
it is NOT semantic understanding and must never be presented as AI output. The UI and the
exports label these results as heuristic.
"""
import re
from collections import Counter
from datetime import date

from docintel_common.grounding import NOT_ENOUGH_INFO
from docintel_common.retrieval import STOPWORDS, normalize_ws, tokenize

# ---------------------------------------------------------------- dates
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
_MON = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|" \
       r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
DATE_PATTERNS = [
    ("mdy", re.compile(rf"\b({_MON})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)),
    ("dmy", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON}),?\s+(\d{{4}})\b", re.I)),
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("numeric", re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")),      # ambiguous: no ISO date
    ("my", re.compile(rf"\b({_MON})\s+(\d{{4}})\b", re.I)),                    # month + year only
]
DATE_TYPE_RULES = [            # first match wins; checked against the whole line, lower-cased
    ("renewal", ("renew",)),
    ("expiration", ("expir", "terminat", "valid until", "valid through")),
    ("effective", ("effective", "commenc", "start date", "starts on")),
    ("payment", ("due", "payment", "payable", "pay by", "late fee")),
    ("deadline", ("deadline", "before", "no later than", "submit", "by ")),
    ("event", ("meeting", "event", "hearing", "appointment", "conference")),
]


def _month(name):
    name = name.lower().rstrip(".")
    return MONTHS.get(name) or MONTHS.get(name[:3])


def _iso(y, m, d):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except (TypeError, ValueError):
        return None


def find_dates(line):
    """Yield ``(as_written, iso_or_None, start)`` for each date in ``line`` (non-overlapping)."""
    taken = []
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(line):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            g = m.groups()
            if kind == "mdy":
                iso = _iso(g[2], _month(g[0]), g[1])
            elif kind == "dmy":
                iso = _iso(g[2], _month(g[1]), g[0])
            elif kind == "iso":
                iso = _iso(g[0], g[1], g[2])
            else:
                iso = None
            taken.append((m.start(), m.end()))
            yield m.group(0), iso, m.start()


def _date_type(line):
    low = line.lower()
    for typ, cues in DATE_TYPE_RULES:
        if any(c in low for c in cues):
            return typ
    return "other"


def _label_of(line, start):
    """'Invoice Date: September 15, 2026' -> 'Invoice Date' (text before a colon that precedes the date)."""
    head = line[:start]
    if ":" in head:
        label = head.rsplit(":", 1)[0].strip()
        label = re.split(r"[.;]\s", label)[-1].strip()
        if 0 < len(label) <= 60:
            return label
    return None


def key_dates(lines):
    """Dates that appear verbatim in the text. ``sourceText`` is the full original line."""
    out, seen = [], set()
    for line in lines:
        for written, iso, start in find_dates(line):
            k = (written.lower(), line)
            if k in seen:
                continue
            seen.add(k)
            typ = _date_type(line)
            label = _label_of(line, start)
            out.append({
                "type": typ,
                "date": written,
                "isoDate": iso,
                "description": label or f"{typ.capitalize()} date mentioned in the text",
                "sourceText": line,
            })
            if len(out) >= 30:
                return out
    return out


# ---------------------------------------------------------------- classification
CATEGORY_CUES = {
    "invoice": ["invoice", "amount due", "bill to", "invoice number", "invoice date", "payment terms", "subtotal",
                "remit"],
    "receipt": ["receipt", "amount paid", "paid", "change due", "thank you for your purchase", "transaction"],
    "contract": ["agreement", "hereby", "party", "parties", "governing law", "terms and conditions", "whereas",
                 "termination", "shall"],
    "resume": ["resume", "curriculum vitae", "work experience", "education", "skills", "references"],
    "report": ["report", "executive summary", "findings", "conclusion", "analysis", "results"],
    "form": ["form", "please complete", "signature", "date of birth", "applicant", "checkbox"],
    "letter": ["dear ", "sincerely", "regards", "yours faithfully", "yours truly"],
    "id_document": ["passport", "driver license", "driver's license", "date of birth", "nationality", "id number"],
}
MIN_CATEGORY_SCORE = 2


def classify(text):
    """Keyword-cue scoring. Returns ``(category, score, matched_cues)``; 'other' when nothing is convincing."""
    low = text.lower()
    best = ("other", 0, [])
    for cat, cues in CATEGORY_CUES.items():
        hits = [c for c in cues if c in low]
        if len(hits) > best[1]:
            best = (cat, len(hits), hits)
    return best if best[1] >= MIN_CATEGORY_SCORE else ("other", best[1], best[2])


# ---------------------------------------------------------------- extractive summary
_AMOUNT = re.compile(r"(?:[$€£₹]\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|INR|%)\b)")
_SALIENT = ("total", "amount", "due", "agreement", "summary", "purpose", "conclusion", "deadline", "renew",
            "payment", "effective", "invoice")


def extractive_summary(lines, max_lines=3):
    """Pick the title line plus the most information-dense lines, verbatim. No generation."""
    lines = [ln for ln in lines if len(ln) >= 4]
    if not lines:
        return ""
    title = lines[0]
    scored = []
    for i, ln in enumerate(lines[1:], start=1):
        low = ln.lower()
        score = 2 * bool(_AMOUNT.search(ln)) + sum(1 for _ in find_dates(ln)) + sum(1 for w in _SALIENT if w in low)
        if score and len(ln) <= 240:
            scored.append((score, -i, ln))
    picked = [ln for _, _, ln in sorted(scored, reverse=True)[: max_lines - 1]]
    picked.sort(key=lambda ln: lines.index(ln))       # keep document order
    return "\n".join([title] + picked)


# ---------------------------------------------------------------- entities and key phrases
def entities(text):
    """Only types that patterns can find reliably: DATE and QUANTITY (amounts). No probability scores."""
    out = []
    for line_start, line in _lines_with_offsets(text):
        for written, _iso_d, start in find_dates(line):
            out.append({"Type": "DATE", "Text": written, "Score": None,
                        "BeginOffset": line_start + start, "EndOffset": line_start + start + len(written)})
        for m in _AMOUNT.finditer(line):
            out.append({"Type": "QUANTITY", "Text": m.group(0).strip(), "Score": None,
                        "BeginOffset": line_start + m.start(), "EndOffset": line_start + m.end()})
    return out


def _lines_with_offsets(text):
    pos = 0
    for line in text.split("\n"):
        yield pos, line
        pos += len(line) + 1


def key_phrases(text, n=12):
    """Most frequent content words (term frequency); ties keep first-appearance order."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text)]
    counts, first, display = Counter(), {}, {}
    for i, w in enumerate(words):
        k = w.lower()
        if k in STOPWORDS or k in {"page", "this", "that", "with", "from", "have", "will"}:
            continue
        counts[k] += 1
        first.setdefault(k, i)
        display.setdefault(k, w)
    ranked = sorted(counts, key=lambda k: (-counts[k], first[k]))[:n]
    top = counts[ranked[0]] if ranked else 1
    return [{"Text": display[k], "Score": round(counts[k] / top, 4)} for k in ranked]


# ---------------------------------------------------------------- PII patterns
def _luhn(digits):
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


PII_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)\s?|\d{3}[-. ])\d{3}[-. ]\d{4}(?!\d)")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("BANK_ACCOUNT_NUMBER", re.compile(r"(?i)\baccount (?:number|no\.?|#)\s*:?\s*(\d{6,18})\b")),
    ("ADDRESS", re.compile(r"\b\d{1,5} [A-Z][a-z]+(?: [A-Z][a-z]+)? (?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Lane|"
                           r"Boulevard|Blvd\.|Drive|Dr\.|Way)\b")),
    ("IN_PERMANENT_ACCOUNT_NUMBER", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
]
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def pii_entities(text):
    """Pattern matches only. Names are NOT detected (that needs an NER model such as Amazon Comprehend).

    ``Score`` is 1.0 because a pattern either matches or it does not; it is not a probability.
    """
    out = []
    for typ, rx in PII_PATTERNS:
        for m in rx.finditer(text):
            g = 1 if rx.groups else 0          # patterns with a group report only the captured value
            out.append({"Type": typ, "Score": 1.0, "BeginOffset": m.start(g), "EndOffset": m.end(g)})
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            out.append({"Type": "CREDIT_DEBIT_NUMBER", "Score": 1.0, "BeginOffset": m.start(), "EndOffset": m.end()})
    out.sort(key=lambda e: e["BeginOffset"])
    return out


# ---------------------------------------------------------------- extractive question answering
def best_line(question, sources):
    """``sources``: ``[(source_id, body_text)]``. Return ``(source_id, line, matched, total_terms)`` or None.

    Picks the single source line sharing the most (stemmed) terms with the question.
    """
    terms = set(tokenize(question))
    if not terms:
        return None
    best = None
    for sid, body in sources:
        for line in body.split("\n"):
            line = line.strip()
            if not line:
                continue
            matched = len(terms & set(tokenize(line)))
            if matched and (best is None or matched > best[2]):
                best = (sid, line, matched, len(terms))
    return best


# question -> field label patterns; checked in this order (more specific first)
FIELD_INTENTS = [
    ("subtotal", r"\bsub-?\s?total\b", r"sub-?\s?total|net amount"),
    ("due date", r"\bdue date\b|\bwhen\b.*\bdue\b|\bdeadline\b|\bpay by\b|\bpayment due\b|\bdue on\b",
     r"due date|payment due|due on|due by"),
    ("invoice date", r"\binvoice date\b|\bwhen was\b.*\b(issued|dated|invoiced)\b|\bdate of (the )?invoice\b",
     r"invoice date|issue date|date of issue"),
    ("invoice number", r"\binvoice (number|no|#|id)\b|\binvoice num", r"invoice (number|no|#|id)"),
    ("tax", r"\b(tax|vat|gst)\b", r"\b(tax|vat|gst)\b"),
    ("discount", r"\bdiscount\b", r"discount"),
    ("shipping", r"\b(shipping|freight|delivery charge)", r"shipping|freight|delivery"),
    ("currency", r"\bcurrency\b", r"currency"),
    ("vendor", r"\b(vendor|supplier|seller)\b|\bwho (issued|sent|billed)\b", r"vendor|supplier|seller"),
    ("payment terms", r"\b(payment )?terms\b", r"terms"),
    ("total", r"\btotal\b|\bamount due\b|\bhow much\b|\bbalance due\b", r"^(grand )?total|amount due|balance"),
]
MIN_COVERAGE = 0.5      # share of question terms a line must contain to be offered as an answer


def _field_answer(question, sources):
    q = question.lower()
    for name, q_rx, key_rx in FIELD_INTENTS:
        if not re.search(q_rx, q):
            continue
        for sid, kind, body in sources:
            if kind != "field" or ":" not in body:
                continue
            key, value = (p.strip() for p in body.split(":", 1))
            if not value or not re.search(key_rx, key.lower()):
                continue
            # cite the verbatim document line that contains the value when it was retrieved too
            needle = normalize_ws(value)
            for sid2, kind2, body2 in sources:
                if kind2 == "field":
                    continue
                line = next((ln.strip() for ln in body2.split("\n") if needle and needle in normalize_ws(ln)), None)
                if line:
                    return {"answer": f'{key}: {value}. Source line: "{line}" [{sid2}]', "answerable": True,
                            "citations": [sid2, sid], "quotes": [{"source": sid2, "quote": line}],
                            "confidence": "medium", "engine": "template"}
            return {"answer": f"{key}: {value} [{sid}] (from the extracted field; the original line was not among "
                              "the retrieved passages)", "answerable": True, "citations": [sid],
                    "quotes": [{"source": sid, "quote": body.strip()}], "confidence": "low", "engine": "template"}
        return None
    return None


def answer(question, sources):
    """``sources``: ``[(source_id, kind, text)]`` from the grounding prompt. Deterministic; never generates facts."""
    hit = _field_answer(question, sources)
    if hit:
        return hit
    best = best_line(question, [(sid, body) for sid, kind, body in sources])
    if best:
        sid, line, matched, total = best
        if matched / total >= MIN_COVERAGE:
            return {"answer": f'Closest matching line in the document: "{line}" [{sid}]', "answerable": True,
                    "citations": [sid], "quotes": [{"source": sid, "quote": line}],
                    "confidence": "medium" if matched >= total else "low", "engine": "extractive"}
    return {"answer": NOT_ENOUGH_INFO, "answerable": False, "citations": [], "quotes": [], "confidence": "low",
            "engine": "none"}
