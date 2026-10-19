"""Explainable invoice anomaly rules (deterministic; no AI, no scoring model).

Every finding names the rule that produced it, the fields and source lines involved, a severity taken from
the fixed table below, a plain-language explanation and a suggested review action. Findings describe
*inconsistencies to check*, never fraud. Values are never changed here; human corrections made through the
field-review workflow are applied (``invoice.effective``) before the rules run, and the original values stay
visible in the evidence.

A finding's id is a hash of the rule, the document and the values involved, so a reviewer's decision stays
attached to exactly the evidence that was reviewed: if the values change, it becomes a new finding.
"""
import hashlib
import os
from decimal import Decimal

from . import invoice

SEVERITIES = ("high", "medium", "low", "info")

RULES = {
    "MISSING_FIELD": {
        "title": "Required field not found",
        "severity": "medium for invoice number or total; low for invoice date or vendor",
        "action": "Check the document and enter the value in Fields & review, or confirm it is genuinely absent."},
    "AMBIGUOUS_FIELD": {
        "title": "Ambiguous value",
        "severity": "low",
        "action": "Pick the correct value and record it as a correction in Fields & review."},
    "INVALID_VALUE": {
        "title": "Value could not be read",
        "severity": "medium",
        "action": "Read the value on the original document and correct it."},
    "ARITHMETIC_MISMATCH": {
        "title": "Amounts do not add up",
        "severity": "high if the difference is more than 1% of the total; otherwise low (possible rounding)",
        "action": "Recalculate from the line items; look for charges the parser does not recognise."},
    "UNEXPLAINED_DIFFERENCE": {
        "title": "Total differs from subtotal with no explanation",
        "severity": "low",
        "action": "Check whether tax or other charges are listed in a form the parser does not recognise."},
    "TAX_RATE_MISMATCH": {
        "title": "Tax does not match the stated rate",
        "severity": "medium",
        "action": "Verify the tax rate and the taxable amount."},
    "DUE_BEFORE_ISSUE": {
        "title": "Due date before invoice date",
        "severity": "medium",
        "action": "Confirm both dates on the original; one of them may be misread or mistyped."},
    "NEGATIVE_AMOUNT": {
        "title": "Negative amount",
        "severity": "medium",
        "action": "Confirm whether this is a credit note or refund."},
    "HIGH_VALUE": {
        "title": "High-value invoice",
        "severity": "info (threshold HIGH_VALUE_THRESHOLD, same currency unit, no conversion)",
        "action": "Apply the approval policy for high-value invoices."},
    "DUPLICATE_INVOICE_NUMBER": {
        "title": "Invoice number used by another document",
        "severity": "medium",
        "action": "Compare the documents; link them as versions if one revises the other."},
    "POSSIBLE_DUPLICATE_SUBMISSION": {
        "title": "Possible duplicate submission",
        "severity": "high (same invoice number, total and invoice date)",
        "action": "Make sure the invoice is not paid twice; dismiss if they are intentionally separate."},
    "VERSION_CHANGE": {
        "title": "Value changed between versions",
        "severity": "info",
        "action": "Confirm the change was expected (e.g. a revised invoice)."},
}
REQUIRED = {"invoiceNumber": "medium", "total": "medium", "invoiceDate": "low", "vendorName": "low"}
VERSIONED_FIELDS = ("invoiceNumber", "invoiceDate", "dueDate", "total", "subtotal", "tax", "paymentTerms")


def high_value_threshold():
    try:
        return Decimal(os.environ.get("HIGH_VALUE_THRESHOLD", "10000"))
    except Exception:
        return Decimal("10000")


def _fid(rule, doc_id, *parts):
    return hashlib.sha256("|".join([rule, doc_id, *map(str, parts)]).encode()).hexdigest()[:16]


def _ev(f):
    return {"field": f["name"], "label": f["label"], "status": f["status"], "value": f.get("raw"),
            "normalized": f.get("value"), "currency": f.get("currency"), "page": f.get("page"),
            "snippet": f.get("evidence"), "original": f.get("original")}


def _finding(rule, doc, severity, explanation, fields, evidence, sig=(), related=None):
    return {"findingId": _fid(rule, doc["docId"], *sig), "rule": rule, "title": RULES[rule]["title"],
            "severity": severity, "severityRule": RULES[rule]["severity"], "docId": doc["docId"],
            "fileName": doc.get("fileName"), "fields": fields, "evidence": evidence, "explanation": explanation,
            "suggestedAction": RULES[rule]["action"], "relatedDocs": related or []}


def invoice_view(item):
    """Effective invoice for an item (corrections applied), or None when the item has none."""
    if not item.get("invoice"):
        return None
    return invoice.effective(item["invoice"], item.get("verifications"))


def _doc_rules(doc, inv):
    out, f = [], inv["fields"]
    for name, sev in REQUIRED.items():
        if f[name]["status"] == "missing":
            out.append(_finding("MISSING_FIELD", doc, sev, f"No {f[name]['label'].lower()} was found in the text.",
                                [name], [_ev(f[name])], (name,)))
    for name, fld in f.items():
        if fld["status"] == "ambiguous":
            out.append(_finding("AMBIGUOUS_FIELD", doc, "low", f"{fld['label']}: {fld.get('note') or 'several values'}"
                                f" ({fld.get('raw')}).", [name], [_ev(fld)], (name, fld.get("raw"))))
        elif fld["status"] == "invalid":
            out.append(_finding("INVALID_VALUE", doc, "medium", f"{fld['label']}: {fld.get('note') or 'unreadable value'}"
                                f" ('{fld.get('raw')}').", [name], [_ev(fld)], (name, fld.get("raw"))))
    for c in inv["checks"]:
        ev = [_ev(f[n]) for n in c["fields"] if n in f]
        sig = (c.get("expected"), c.get("found"))
        if c["id"] == "arithmetic" and c["status"] == "fail":
            pct = Decimal(c.get("differencePercent") or "100")
            sev = "high" if pct > 1 else "low"
            out.append(_finding("ARITHMETIC_MISMATCH", doc, sev, c["message"], c["fields"], ev, sig))
        elif c["id"] == "arithmetic" and c["status"] == "warn":
            out.append(_finding("UNEXPLAINED_DIFFERENCE", doc, "low", c["message"], c["fields"], ev, sig))
        elif c["id"] == "taxRate" and c["status"] == "fail":
            out.append(_finding("TAX_RATE_MISMATCH", doc, "medium", c["message"], c["fields"], ev, sig))
        elif c["id"] == "dateOrder" and c["status"] == "fail":
            out.append(_finding("DUE_BEFORE_ISSUE", doc, "medium", c["message"], c["fields"], ev,
                                (f["invoiceDate"].get("value"), f["dueDate"].get("value"))))
        elif c["id"] == "negativeAmounts" and c["status"] == "warn":
            out.append(_finding("NEGATIVE_AMOUNT", doc, "medium", c["message"], c["fields"], ev,
                                tuple(f[n].get("value") for n in c["fields"])))
    total = f["total"]
    if total["status"] in invoice.USABLE and total.get("value"):
        thr = high_value_threshold()
        if Decimal(total["value"]) >= thr:
            out.append(_finding("HIGH_VALUE", doc, "info", f"Total {total['value']} {total.get('currency') or ''} is at "
                                f"or above the configured threshold of {thr}.".replace("  ", " "), ["total"],
                                [_ev(total)], (total["value"], str(thr))))
    return out


def _usable(inv, name):
    fld = inv["fields"][name]
    return fld.get("value") if fld["status"] in invoice.USABLE else None


def detect(items, decisions=None):
    """Run every rule over ``items`` (Documents-table items, Decimal-free). ``decisions``: findingId -> record."""
    decisions = decisions or {}
    docs = []
    for it in items:
        if it.get("status") != "COMPLETED":
            continue
        inv = invoice_view(it)
        if inv and invoice.is_invoice_like(it, inv):
            docs.append((it, inv))

    findings = []
    for doc, inv in docs:
        findings.extend(_doc_rules(doc, inv))

    # cross-document: the same invoice number on different documents (not versions of each other)
    by_number = {}
    for doc, inv in docs:
        num = _usable(inv, "invoiceNumber")
        if num:
            by_number.setdefault(num.upper(), []).append((doc, inv))
    for num, group in by_number.items():
        for doc, inv in group:
            others = []
            for d2, i2 in group:
                if d2["docId"] == doc["docId"]:
                    continue
                if doc.get("versionGroup") and doc.get("versionGroup") == d2.get("versionGroup"):
                    continue
                v1, v2 = _usable(inv, "vendorName"), _usable(i2, "vendorName")
                if v1 and v2 and v1.strip().lower() != v2.strip().lower():
                    continue                       # different vendors may legitimately reuse numbers
                others.append((d2, i2))
            if not others:
                continue
            my_total, my_date = _usable(inv, "total"), _usable(inv, "invoiceDate")
            same = [d2 for d2, i2 in others if my_total is not None and _usable(i2, "total") == my_total
                    and _usable(i2, "invoiceDate") == my_date]
            related = [{"docId": d2["docId"], "fileName": d2.get("fileName")} for d2, _ in others]
            ev = [_ev(inv["fields"]["invoiceNumber"])]
            ids = sorted(r["docId"] for r in related)
            if same:
                findings.append(_finding("POSSIBLE_DUPLICATE_SUBMISSION", doc, "high",
                                         f"Invoice number {num}, total and invoice date also appear on "
                                         f"{', '.join(d.get('fileName') or d['docId'] for d in same)}.",
                                         ["invoiceNumber", "total", "invoiceDate"], ev, (num, *ids), related))
            else:
                findings.append(_finding("DUPLICATE_INVOICE_NUMBER", doc, "medium",
                                         f"Invoice number {num} also appears on "
                                         f"{', '.join(r['fileName'] or r['docId'] for r in related)} with different "
                                         "amounts or dates.", ["invoiceNumber"], ev, (num, *ids), related))

    # versions: report what changed from the previous version (a detected difference, not a judgement)
    groups = {}
    for doc, inv in docs:
        if doc.get("versionGroup"):
            groups.setdefault(doc["versionGroup"], []).append((doc, inv))
    for group in groups.values():
        group.sort(key=lambda x: x[0].get("versionNumber") or 0)
        for (prev, pinv), (cur, cinv) in zip(group, group[1:]):
            for name in VERSIONED_FIELDS:
                a, b = _usable(pinv, name), _usable(cinv, name)
                if a is not None and b is not None and a != b:
                    label = cinv["fields"][name]["label"]
                    findings.append(_finding(
                        "VERSION_CHANGE", cur, "info",
                        f"{label} changed from {pinv['fields'][name].get('raw')} (version {prev.get('versionNumber')}) "
                        f"to {cinv['fields'][name].get('raw')} (version {cur.get('versionNumber')}).",
                        [name], [_ev(pinv["fields"][name]), _ev(cinv["fields"][name])], (name, a, b, prev["docId"]),
                        [{"docId": prev["docId"], "fileName": prev.get("fileName")}]))

    for fd in findings:
        d = decisions.get(fd["findingId"])
        fd["review"] = d
        fd["status"] = d["decision"] if d else "open"
    findings.sort(key=lambda x: (SEVERITIES.index(x["severity"]), x.get("fileName") or "", x["rule"]))
    return findings


def summarize(findings):
    out = {"total": len(findings), "open": 0, "acknowledged": 0, "dismissed": 0,
           "openBySeverity": {s: 0 for s in SEVERITIES}}
    for f in findings:
        out[f["status"]] = out.get(f["status"], 0) + 1
        if f["status"] == "open":
            out["openBySeverity"][f["severity"]] += 1
    return out
