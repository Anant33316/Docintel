"""Deterministic invoice field extraction and arithmetic validation. No AI, no network.

Input is the processor's ``pageLines`` structure (``[{"page": n, "lines": [{"text": ...}]}]``), so it works
on Textract output in AWS and on PyMuPDF text locally. Every value keeps its original text (``raw``), the
page and the full source line (``evidence``). Nothing is guessed silently:

* ``extracted`` - found next to an explicit label ("Invoice Number:", "Total Amount Due", ...)
* ``inferred``  - derived without a label (vendor from the heading, currency from amount symbols)
* ``ambiguous`` - several different candidates, or a value that can be read two ways (04/05/2026)
* ``invalid``   - a label was found but its value cannot be parsed (e.g. "February 30, 2026")
* ``missing``   - not found
"""
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

METHOD = "deterministic-parser"
VERSION = 1
TOLERANCE = Decimal("0.01")          # rounding tolerance for arithmetic checks (one cent / smallest unit)
TAX_TOLERANCE = Decimal("0.02")      # tax lines may be rounded per item

FIELD_LABELS = {
    "invoiceNumber": "Invoice number", "vendorName": "Vendor", "invoiceDate": "Invoice date",
    "dueDate": "Due date", "currency": "Currency", "subtotal": "Subtotal", "tax": "Tax",
    "discount": "Discount", "shipping": "Shipping / handling", "total": "Total amount",
    "paymentTerms": "Payment terms",
}
FIELD_ORDER = list(FIELD_LABELS)
MONEY_FIELDS = ("subtotal", "tax", "discount", "shipping", "total")
DATE_FIELDS = ("invoiceDate", "dueDate")
USABLE = ("extracted", "inferred", "corrected")

# ---------------------------------------------------------------- labels
# (field, priority, pattern, needs_colon). Lower priority wins when a field has several candidates.
# field None = a delimiter: it ends the previous label's value but produces nothing.
_LABELS = [
    ("total", 1, r"grand\s+total", False), ("total", 1, r"total\s+amount\s+due", False),
    ("total", 1, r"total\s+due", False), ("total", 1, r"amount\s+due", False),
    ("total", 2, r"balance\s+due", False), ("total", 2, r"total\s+amount", False),
    ("total", 2, r"invoice\s+total", False), ("total", 2, r"amount\s+payable", False),
    ("subtotal", 1, r"sub[\s-]?total", False), ("subtotal", 2, r"amount\s+before\s+tax", False),
    ("subtotal", 2, r"net\s+amount", False),
    ("total", 3, r"total", False),
    ("tax", 1, r"(?:sales\s+tax|vat|igst|cgst|sgst|gst|tax)(?!\s*(?:id|no\b|number|reg|registration|exempt|"
               r"identification|invoice|in\b))", False),
    ("discount", 1, r"discount", False),
    ("shipping", 1, r"shipping(?:\s*(?:&|and)\s*handling)?|freight|delivery\s+charges?|handling", False),
    ("invoiceNumber", 1, r"invoice\s*(?:number|no\.?|num|#|id)|inv\.?\s*(?:no\.?|#)", False),
    ("invoiceDate", 1, r"invoice\s+date|date\s+of\s+issue|issue\s+date|issued\s+on|billing\s+date", False),
    ("dueDate", 1, r"due\s+date|payment\s+due(?:\s+date)?|due\s+on|due\s+by|pay\s+by", False),
    ("paymentTerms", 1, r"payment\s+terms|terms\s+of\s+payment", False),
    ("paymentTerms", 2, r"terms", True),
    ("currency", 1, r"currency", True),
    ("vendorName", 1, r"vendor|supplier|seller|billed\s+by|issued\s+by|remit\s+to|payable\s+to", True),
    ("vendorName", 2, r"from", True),
    ("invoiceDate", 3, r"date", True),
    (None, 9, r"bill\s+to|ship\s+to|sold\s+to|customer|e-?mail|phone|tel|fax|account\s+(?:number|no\.?)|"
              r"po\s+(?:number|no\.?)|purchase\s+order|reference|services|description|qty|quantity|unit\s+price|"
              r"late\s+fee", False),
]
# longest alternatives first so "total amount due" wins over "total" at the same position
_ORDERED = sorted(enumerate(_LABELS), key=lambda x: -len(x[1][2]))
_LABEL_RX = re.compile("|".join(f"(?P<l{i}>(?<![A-Za-z]){pat}(?![A-Za-z]))" for i, (_, _, pat, _) in _ORDERED), re.I)
# a value region may contain these labels without ending (payment terms often mention the due date)
_ABSORBS = {"paymentTerms": {"dueDate"}}

# ---------------------------------------------------------------- dates
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], start=1)}
_MON = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
_DATE_RX = [
    ("mdy", re.compile(rf"\b({_MON})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)),
    ("dmy", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON}),?\s+(\d{{4}})\b", re.I)),
    ("iso", re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")),
    ("num", re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b")),
]


def _mk(y, m, d):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except (TypeError, ValueError):
        return None


def parse_date(text):
    """First date in ``text`` -> ``{"raw", "value", "status", "note", "alternatives"}`` or None if none."""
    hits = []
    for kind, rx in _DATE_RX:
        m = rx.search(text or "")
        if m:
            hits.append((m.start(), kind, m))
    if not hits:
        return None
    _, kind, m = min(hits, key=lambda h: h[0])
    g, raw = m.groups(), m.group(0)
    out = {"raw": raw, "value": None, "status": "ok", "note": None, "alternatives": None}
    if kind == "mdy":
        out["value"] = _mk(g[2], _MONTHS[g[0].lower()[:3]], g[1])
    elif kind == "dmy":
        out["value"] = _mk(g[2], _MONTHS[g[1].lower()[:3]], g[0])
    elif kind == "iso":
        out["value"] = _mk(g[0], g[1], g[2])
    else:
        a, b, y = int(g[0]), int(g[1]), g[2]
        if len(y) == 2:
            y = f"20{y}"
            out["note"] = f"two-digit year read as {y}"
        dmy, mdy = _mk(y, b, a), _mk(y, a, b)
        if dmy and mdy and dmy != mdy:
            out.update(status="ambiguous", alternatives=[dmy, mdy],
                       note=f"'{raw}' can be read as day/month ({dmy}) or month/day ({mdy})")
            return out
        out["value"] = dmy or mdy
    if out["value"] is None:
        out.update(status="invalid", note=f"'{raw}' is not a valid calendar date")
    return out


# ---------------------------------------------------------------- money
_SYMBOLS = {"$": ("USD", "'$' assumed to mean USD"), "€": ("EUR", None), "£": ("GBP", None),
            "₹": ("INR", None), "¥": ("JPY", "'¥' assumed to mean JPY")}
_CODES = "USD|EUR|GBP|INR|JPY|CAD|AUD|CHF|CNY|SGD|AED|NZD|ZAR"
_MONEY_RX = re.compile(
    rf"(?P<open>\()?(?P<neg>-)?\s*(?P<pre>[$€£₹¥]|\b(?:{_CODES})\b|\bRs\.?)?\s*(?P<neg2>-)?"
    rf"(?P<num>\d[\d,.]*\d|\d)(?![\d.,]*\s*%)(?!\d)\s*(?P<post>[$€£₹¥]|\b(?:{_CODES})\b)?(?P<close>\))?", re.I)


def _to_decimal(num):
    """'4,800' -> 4800; '1.234,56' -> 1234.56; '12,50' -> 12.50. Returns (Decimal|None, ambiguity_note)."""
    try:
        if "," in num and "." in num:
            dec = max(num.rfind(","), num.rfind("."))
            sep = num[dec]
            whole = num[:dec].replace(",", "").replace(".", "")
            return Decimal(f"{whole}.{num[dec + 1:]}"), None if sep == "." else None
        if "," in num:
            parts = num.split(",")
            if len(parts) == 2 and len(parts[1]) in (1, 2):
                return Decimal(f"{parts[0]}.{parts[1]}"), "comma read as decimal separator"
            if len(parts[-1]) == 3 and all(len(p) in (2, 3) for p in parts[1:-1]):
                return Decimal("".join(parts)), None
            return None, None
        if "." in num:
            parts = num.split(".")
            if len(parts) > 2:
                if all(len(p) == 3 for p in parts[1:]):
                    return Decimal("".join(parts)), "dots read as thousands separators"
                return None, None
            if len(parts[1]) == 3:
                return Decimal(num), f"'{num}' could also mean {int(parts[0]) * 1000 + int(parts[1])}"
        return Decimal(num), None
    except InvalidOperation:
        return None, None


def parse_money(text):
    """First amount in ``text`` -> ``{"raw", "value", "currency", "status", "note"}`` or None."""
    for m in _MONEY_RX.finditer(text or ""):
        amount, note = _to_decimal(m.group("num"))
        if amount is None:
            continue
        cur_tok = m.group("pre") or m.group("post")
        currency, cur_note = None, None
        if cur_tok:
            t = cur_tok.strip().upper().rstrip(".")
            if t in ("RS",):
                currency = "INR"
            elif cur_tok in _SYMBOLS:
                currency, cur_note = _SYMBOLS[cur_tok]
            else:
                currency = t
        negative = bool(m.group("neg") or m.group("neg2") or (m.group("open") and m.group("close")))
        if negative:
            amount = -amount
        ambiguous = bool(note and "could also mean" in note)
        notes = "; ".join(n for n in (note, cur_note) if n) or None
        return {"raw": m.group(0).strip(), "value": amount, "currency": currency,
                "status": "ambiguous" if ambiguous else "ok", "note": notes}
    return None


def money_str(d):
    return format(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f") if isinstance(d, Decimal) else d


def _dec(v):
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- extraction
def _regions(line):
    """Yield (field, priority, region_text) for every label in the line."""
    hits = []
    for m in _LABEL_RX.finditer(line):
        idx = int(next(k for k, v in m.groupdict().items() if v is not None)[1:])
        field, prio, _pat, needs_colon = _LABELS[idx]
        rest = line[m.end():]
        if needs_colon and not re.match(r"\s*[:#]", rest):
            continue
        hits.append((m.start(), m.end(), field, prio))
    for i, (start, end, field, prio) in enumerate(hits):
        if field is None:
            continue
        stop = len(line)
        for s2, _e2, f2, _p2 in hits[i + 1:]:
            if f2 not in _ABSORBS.get(field, ()):
                stop = s2
                break
        region = line[end:stop]
        rate = re.search(r"(\d+(?:\.\d+)?)\s*%", region) if field == "tax" else None
        yield field, prio, re.sub(r"^(?:[\s:#.]|[-–](?=\s))+", "", region).strip(), (rate.group(1) if rate else None)


def _candidate(field, region):
    if field in MONEY_FIELDS:
        m = parse_money(region)
        if not m:
            return None
        return {"raw": m["raw"], "value": money_str(m["value"]), "currency": m["currency"],
                "status": m["status"], "note": m["note"]}
    if field in DATE_FIELDS:
        d = parse_date(region)
        if not d:
            return None
        return {"raw": d["raw"], "value": d["value"], "status": d["status"], "note": d["note"],
                "alternatives": d["alternatives"]}
    if field == "invoiceNumber":
        m = re.search(r"[A-Za-z0-9][A-Za-z0-9\-/_.]*[A-Za-z0-9]|\d", region)
        if not m or not re.search(r"\d", m.group(0)):
            return None
        return {"raw": m.group(0), "value": m.group(0).upper(), "status": "ok", "note": None}
    if field == "currency":
        m = re.search(rf"\b({_CODES})\b", region, re.I)
        return {"raw": m.group(0), "value": m.group(1).upper(), "status": "ok", "note": None} if m else None
    if field == "paymentTerms":
        text = region[:160].strip().rstrip(".")
        return {"raw": text, "value": text, "status": "ok", "note": None} if text else None
    if field == "vendorName":
        text = re.split(r"\s{2,}|,|\|", region)[0].strip().rstrip(".")
        return {"raw": text, "value": text, "status": "ok", "note": None} if len(text) >= 2 else None
    return None


def _heading_vendor(page_lines):
    """Vendor from the first heading line, e.g. 'Northwind Cloud Consulting - Invoice'. Marked 'inferred'."""
    first = next((p for p in page_lines if p.get("lines")), None)
    if not first:
        return None
    for ln in first["lines"][:4]:
        text = (ln.get("text") or "").strip()
        if not text or ":" in text or sum(c.isdigit() for c in text) > len(text) / 3:
            continue
        name = re.sub(r"(?i)\b(?:tax\s+)?invoice\b", "", text)
        name = re.sub(r"^[\s\-–|:]+|[\s\-–|:]+$", "", name).strip()
        if len(name) >= 2 and re.search(r"[A-Za-z]{2}", name):
            return {"raw": name, "value": name, "page": first.get("page"), "evidence": text}
    return None


def _field(name, status, **kw):
    out = {"name": name, "label": FIELD_LABELS[name], "status": status, "raw": None, "value": None,
           "page": None, "evidence": None, "note": None}
    out.update({k: v for k, v in kw.items() if v is not None})
    return out


def extract(page_lines):
    """Return ``{"method", "version", "fields": {name: field}, "checks": [...]}``."""
    cands = {}
    for page in page_lines or []:
        for ln in page.get("lines", []):
            text = (ln.get("text") or "").strip()
            if not text:
                continue
            for field, prio, region, rate in _regions(text):
                c = _candidate(field, region)
                if c:
                    c.update(priority=prio, page=page.get("page"), evidence=text)
                    if rate:
                        c["taxRate"] = rate
                    cands.setdefault(field, []).append(c)

    fields = {}
    for name in FIELD_ORDER:
        lst = cands.get(name) or []
        if not lst:
            fields[name] = _field(name, "missing")
            continue
        best = min(c["priority"] for c in lst)
        top = [c for c in lst if c["priority"] == best]
        distinct = {}
        for c in top:
            distinct.setdefault((c["value"], c.get("currency")), c)
        if len(distinct) > 1:
            fields[name] = _field(name, "ambiguous", raw=" / ".join(c["raw"] for c in distinct.values()),
                                  page=top[0]["page"], evidence=top[0]["evidence"],
                                  note=f"{len(distinct)} different values found under the same label",
                                  candidates=[{k: c.get(k) for k in ("raw", "value", "currency", "page", "evidence")}
                                              for c in distinct.values()])
            continue
        c = top[0]
        status = {"ok": "extracted", "ambiguous": "ambiguous", "invalid": "invalid"}[c["status"]]
        fields[name] = _field(name, status, raw=c["raw"], value=c["value"], page=c["page"], evidence=c["evidence"],
                              note=c.get("note"), currency=c.get("currency"), taxRate=c.get("taxRate"),
                              alternatives=c.get("alternatives"))

    if fields["vendorName"]["status"] == "missing":
        v = _heading_vendor(page_lines or [])
        if v:
            fields["vendorName"] = _field("vendorName", "inferred", note="Inferred from the document heading; "
                                          "no 'Vendor:'-style label was found.", **v)
    if fields["currency"]["status"] == "missing":
        found = {}
        for n in MONEY_FIELDS:
            f = fields[n]
            if f.get("currency") and f["status"] in USABLE:
                found.setdefault(f["currency"], f)
        if len(found) == 1:
            cur, src = next(iter(found.items()))
            fields["currency"] = _field("currency", "inferred", raw=src.get("raw"), value=cur, page=src.get("page"),
                                        evidence=src.get("evidence"),
                                        note="Inferred from the currency written next to the amounts.")
        elif len(found) > 1:
            fields["currency"] = _field("currency", "ambiguous", raw=" / ".join(found), page=None,
                                        note="Amounts are written in different currencies: " + ", ".join(found))
    return {"method": METHOD, "version": VERSION, "fields": fields, "checks": validate(fields)}


# ---------------------------------------------------------------- validation
def _num(fields, name):
    f = fields.get(name) or {}
    return _dec(f.get("value")) if f.get("status") in USABLE else None


def _check(cid, status, message, fields, **kw):
    return {"id": cid, "status": status, "message": message, "fields": fields,
            **{k: v for k, v in kw.items() if v is not None}}


def validate(fields):
    """Deterministic consistency checks. Status: pass | fail | warn | skipped. Values are never modified."""
    checks = []
    total, sub = _num(fields, "total"), _num(fields, "subtotal")
    tax, ship, disc = _num(fields, "tax"), _num(fields, "shipping"), _num(fields, "discount")
    curs = {(fields.get(n) or {}).get("currency") for n in MONEY_FIELDS if _num(fields, n) is not None} - {None}

    if total is None or sub is None:
        checks.append(_check("arithmetic", "skipped", "Needs both a subtotal and a total to check the arithmetic.",
                             ["subtotal", "total"]))
    elif len(curs) > 1:
        checks.append(_check("arithmetic", "skipped", "Amounts use different currencies; not compared.",
                             list(MONEY_FIELDS)))
    else:
        parts = [("subtotal", sub)] + [(n, v) for n, v in (("tax", tax), ("shipping", ship)) if v is not None]
        expected = sub + (tax or 0) + (ship or 0) - (abs(disc) if disc is not None else 0)
        formula = " + ".join(f"{n} {money_str(v)}" for n, v in parts) + \
            (f" - discount {money_str(abs(disc))}" if disc is not None else "")
        diff = total - expected
        used = [n for n, _ in parts] + (["discount"] if disc is not None else []) + ["total"]
        if abs(diff) <= TOLERANCE:
            checks.append(_check("arithmetic", "pass", f"{formula} = {money_str(expected)}, matching the total.", used,
                                 expected=money_str(expected), found=money_str(total)))
        elif tax is None and ship is None and disc is None:
            checks.append(_check("arithmetic", "warn",
                                 f"The total ({money_str(total)}) differs from the subtotal ({money_str(sub)}) by "
                                 f"{money_str(diff)}, and no tax, discount or shipping line was found to explain it.",
                                 used, expected=money_str(expected), found=money_str(total), difference=money_str(diff)))
        else:
            pct = (abs(diff) / abs(total) * 100) if total else None
            checks.append(_check("arithmetic", "fail",
                                 f"{formula} = {money_str(expected)}, but the total is {money_str(total)} "
                                 f"(difference {money_str(diff)}). The document may contain charges this parser "
                                 "does not recognise.", used, expected=money_str(expected), found=money_str(total),
                                 difference=money_str(diff),
                                 differencePercent=f"{pct:.2f}" if pct is not None else None))

    rate = (fields.get("tax") or {}).get("taxRate")
    if tax is not None and sub is not None and rate:
        r = Decimal(rate) / 100
        bases = [sub] + ([sub - abs(disc)] if disc is not None else [])
        options = [(b * r).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for b in bases]
        if any(abs(tax - o) <= TAX_TOLERANCE for o in options):
            checks.append(_check("taxRate", "pass", f"Tax {money_str(tax)} matches {rate}% of the taxable amount.",
                                 ["tax", "subtotal"]))
        else:
            checks.append(_check("taxRate", "fail", f"{rate}% of {money_str(bases[0])} is {money_str(options[0])}, but "
                                 f"the tax line says {money_str(tax)}.", ["tax", "subtotal"],
                                 expected=money_str(options[0]), found=money_str(tax)))

    inv, due = fields.get("invoiceDate") or {}, fields.get("dueDate") or {}
    if inv.get("status") in USABLE and due.get("status") in USABLE and inv.get("value") and due.get("value"):
        if due["value"] < inv["value"]:
            checks.append(_check("dateOrder", "fail", f"The due date ({due['value']}) is before the invoice date "
                                 f"({inv['value']}).", ["invoiceDate", "dueDate"]))
        else:
            checks.append(_check("dateOrder", "pass", "The due date is on or after the invoice date.",
                                 ["invoiceDate", "dueDate"]))

    negatives = [n for n in MONEY_FIELDS if n != "discount" and (_num(fields, n) or 0) < 0]
    if negatives:
        checks.append(_check("negativeAmounts", "warn", "Negative amount(s): " + ", ".join(negatives) +
                             ". This can be legitimate (credit note, refund) but should be confirmed.", negatives))
    return checks


# ---------------------------------------------------------------- human corrections and review fields
FIELD_ID_PREFIX = "inv_"


def field_id(name):
    return f"{FIELD_ID_PREFIX}{name}"


def normalize_value(name, raw):
    """Re-parse a human-entered value with the same rules as extraction (used for corrections)."""
    if name in MONEY_FIELDS:
        m = parse_money(raw)
        return (money_str(m["value"]), m["currency"]) if m else (None, None)
    if name in DATE_FIELDS:
        d = parse_date(raw)
        return ((d["value"] if d and d["status"] == "ok" else None), None)
    if name == "invoiceNumber":
        return raw.strip().upper(), None
    if name == "currency":
        return raw.strip().upper()[:3], None
    return raw.strip(), None


def effective(invoice, verifications=None):
    """Invoice with human decisions applied (corrections re-parsed), checks recomputed. Originals are kept."""
    if not invoice:
        return None
    verifications = verifications or {}
    fields = {}
    for name in FIELD_ORDER:
        f = dict((invoice.get("fields") or {}).get(name) or _field(name, "missing"))
        v = verifications.get(field_id(name)) or {}
        if v.get("status") == "CORRECTED" and v.get("correctedValue"):
            value, cur = normalize_value(name, v["correctedValue"])
            f["original"] = {k: f.get(k) for k in ("status", "raw", "value", "currency")}
            f.update(status="corrected" if value is not None else "invalid", raw=v["correctedValue"], value=value,
                     reviewedBy=v.get("verifiedBy"), note="Corrected by a reviewer")
            if cur:
                f["currency"] = cur
        elif v.get("status") == "VERIFIED":
            f["confirmedBy"] = v.get("verifiedBy")
        fields[name] = f
    return {**invoice, "fields": fields, "checks": validate(fields)}


def review_fields(invoice):
    """Invoice fields shaped like processor ``fields`` (no confidence: they come from text rules, not OCR)."""
    out = []
    for name in FIELD_ORDER:
        f = invoice["fields"][name]
        if f["status"] == "missing":
            continue
        # what a reviewer confirms: the text as written, except currency (inferred from symbols -> show the code)
        value = (f.get("value") if name == "currency" and f["status"] != "ambiguous" else f.get("raw")) or ""
        out.append({"fieldId": field_id(name), "key": f["label"], "value": value, "keyConfidence": None,
                    "valueConfidence": None, "confidence": None, "page": f.get("page"),
                    "source": METHOD, "parseStatus": f["status"], "evidence": f.get("evidence")})
    return out


def is_invoice_like(item, invoice):
    """Only invoice-like documents get invoice checks (avoids flagging contracts or letters)."""
    f = (invoice or {}).get("fields") or {}
    return item.get("category") == "invoice" or (f.get("invoiceNumber") or {}).get("status") in USABLE
