"""Unit tests: deterministic invoice parsing and validation (pure functions, no AWS, no extra packages)."""
import pytest
from docintel_common import invoice as inv


def pages(*pgs):
    return [{"page": i, "lines": [{"text": t} for t in lines]} for i, lines in enumerate(pgs, start=1)]


def run(*lines):
    return inv.extract(pages(list(lines)))


def check(result, cid):
    return next((c for c in result["checks"] if c["id"] == cid), None)


NORTHWIND = ["Northwind Cloud Consulting - Invoice", "Invoice Number: NW-2026-001", "Invoice Date: September 15, 2026",
             "Bill To: Priya Sharma, 42 Market Street, Springfield", "Total Amount Due: 4,800 USD",
             "Payment Terms: Net 30. Payment is due by October 15, 2026."]
FULL = ["INVOICE", "Globex Supplies Ltd", "Invoice No. INV/2026/77", "Invoice Date: 2026-04-03", "Due Date: 15.05.2026",
        "Subtotal: $1,250.00", "Discount: -50.00", "Tax (18%): $216.00", "Shipping & Handling: $25.00",
        "Grand Total: $1,441.00", "Terms: Net 45"]


def test_fields_keep_raw_value_page_and_evidence():
    r = inv.extract(pages(["Cover page"], NORTHWIND))
    f = r["fields"]
    assert f["invoiceNumber"]["status"] == "extracted" and f["invoiceNumber"]["value"] == "NW-2026-001"
    assert f["invoiceNumber"]["page"] == 2 and f["invoiceNumber"]["evidence"] == "Invoice Number: NW-2026-001"
    assert f["invoiceDate"]["value"] == "2026-09-15" and f["invoiceDate"]["raw"] == "September 15, 2026"
    assert f["dueDate"]["value"] == "2026-10-15"                         # found inside the payment-terms sentence
    assert f["total"]["value"] == "4800.00" and f["total"]["raw"] == "4,800 USD" and f["total"]["currency"] == "USD"
    assert f["paymentTerms"]["value"].startswith("Net 30")
    assert f["currency"]["status"] == "inferred" and f["currency"]["value"] == "USD"
    assert f["subtotal"]["status"] == "missing" and f["subtotal"]["value"] is None       # never invented
    assert check(r, "arithmetic")["status"] == "skipped"


def test_vendor_from_heading_is_inferred_not_extracted():
    assert run(*NORTHWIND)["fields"]["vendorName"] == {
        "name": "vendorName", "label": "Vendor", "status": "inferred", "raw": "Northwind Cloud Consulting",
        "value": "Northwind Cloud Consulting", "page": 1, "evidence": "Northwind Cloud Consulting - Invoice",
        "note": "Inferred from the document heading; no 'Vendor:'-style label was found."}
    assert run("INVOICE", "Vendor: Initech LLC", "Total: 5")["fields"]["vendorName"]["status"] == "extracted"


def test_full_arithmetic_with_discount_shipping_and_tax_rate():
    r = run(*FULL)
    assert check(r, "arithmetic")["status"] == "pass" and check(r, "arithmetic")["expected"] == "1441.00"
    assert check(r, "taxRate")["status"] == "pass"
    assert r["fields"]["discount"]["value"] == "-50.00"          # sign preserved; subtracted as a discount
    assert check(r, "dateOrder")["status"] == "pass"


def test_wrong_total_is_flagged_with_the_difference():
    r = run(*[ln.replace("1,441.00", "1,541.00") for ln in FULL])
    a = check(r, "arithmetic")
    assert a["status"] == "fail" and a["difference"] == "100.00" and a["expected"] == "1441.00"
    assert "1541.00" in a["message"]


def test_rounding_within_one_cent_passes():
    assert check(run("Subtotal: 99.99", "VAT: 20.00", "Total: 119.99"), "arithmetic")["status"] == "pass"
    assert check(run("Subtotal: 99.99", "VAT: 20.00", "Total: 119.98"), "arithmetic")["status"] == "pass"  # 0.01
    assert check(run("Subtotal: 99.99", "VAT: 20.00", "Total: 120.50"), "arithmetic")["status"] == "fail"


def test_unexplained_difference_is_a_warning_not_an_error():
    r = run("Subtotal: 100.00", "Total: 118.00")
    a = check(r, "arithmetic")
    assert a["status"] == "warn" and "no tax, discount or shipping" in a["message"]


def test_tax_rate_mismatch():
    r = run("Subtotal: 1000.00", "GST (18%): 150.00", "Total: 1150.00")
    assert check(r, "arithmetic")["status"] == "pass" and check(r, "taxRate")["status"] == "fail"


@pytest.mark.parametrize("text,value,status", [
    ("Invoice Date: 2026-01-05", "2026-01-05", "extracted"),
    ("Invoice Date: 5 January 2026", "2026-01-05", "extracted"),
    ("Invoice Date: Jan. 5th, 2026", "2026-01-05", "extracted"),
    ("Invoice Date: 25/12/2026", "2026-12-25", "extracted"),           # day > 12: unambiguous
    ("Invoice Date: 12/25/2026", "2026-12-25", "extracted"),
    ("Invoice Date: 04/05/2026", None, "ambiguous"),                   # 4 May or April 5
    ("Invoice Date: 31/02/2026", None, "invalid"),                     # no such date
    ("Invoice Date: February 30, 2026", None, "invalid"),
])
def test_date_formats(text, value, status):
    f = run(text)["fields"]["invoiceDate"]
    assert (f["status"], f["value"]) == (status, value)
    if status == "ambiguous":
        assert f["alternatives"] == ["2026-05-04", "2026-04-05"] and "day/month" in f["note"]


def test_two_digit_year_is_noted():
    f = run("Invoice Date: 25/12/26")["fields"]["invoiceDate"]
    assert f["value"] == "2026-12-25" and "two-digit year" in f["note"]


@pytest.mark.parametrize("text,amount,currency,status", [
    ("Total: $1,234.56", "1234.56", "USD", "ok"),
    ("Total: €1.234,56", "1234.56", "EUR", "ok"),
    ("Total: 1 250 EUR", None, None, None),                 # space grouping is not guessed
    ("Total: Rs. 5,00,000.00", "500000.00", "INR", "ok"),
    ("Total: INR 1,23,456", "123456.00", "INR", "ok"),
    ("Total: (120.00)", "-120.00", None, "ok"),
    ("Total: £12,50", "12.50", "GBP", "ok"),
    ("Total: 1.234", "1.23", None, "ambiguous"),             # 1.234 or 1234?
])
def test_money_formats(text, amount, currency, status):
    m = inv.parse_money(text.split(": ", 1)[1])
    if amount is None:
        assert m["value"] != 1250                            # read as 1 (first token), never as 1250
        return
    assert inv.money_str(m["value"]) == amount and m["currency"] == currency and m["status"] == status


def test_percentages_are_not_amounts():
    f = run("Tax (18%): 864.00")["fields"]["tax"]
    assert f["value"] == "864.00" and f["taxRate"] == "18"


def test_conflicting_values_are_ambiguous_not_picked():
    f = run("Total: 100.00", "Some text", "Total: 120.00")["fields"]["total"]
    assert f["status"] == "ambiguous" and f["value"] is None and len(f["candidates"]) == 2
    assert check(run("Subtotal: 100", "Total: 100.00", "Total: 120.00"), "arithmetic")["status"] == "skipped"


def test_stronger_label_wins_over_weaker():
    f = run("Total: 4,000 USD", "Total Amount Due: 4,800 USD")["fields"]["total"]
    assert f["status"] == "extracted" and f["value"] == "4800.00"


def test_tax_id_and_subtotal_are_not_confused():
    r = run("Tax ID: 12-3456789", "Sub-total: 50.00", "Total: 50.00")
    assert r["fields"]["tax"]["status"] == "missing"
    assert r["fields"]["subtotal"]["value"] == "50.00" and r["fields"]["total"]["value"] == "50.00"
    assert check(r, "arithmetic")["status"] == "pass"


def test_several_labels_on_one_line():
    f = run("Invoice Date: 2026-01-05    Due Date: 2026-02-04")["fields"]
    assert f["invoiceDate"]["value"] == "2026-01-05" and f["dueDate"]["value"] == "2026-02-04"


def test_due_before_invoice_date_and_negative_total():
    r = run("Invoice Date: 2026-03-10", "Due Date: 2026-03-01", "Total: -40.00")
    assert check(r, "dateOrder")["status"] == "fail"
    assert check(r, "negativeAmounts")["status"] == "warn" and "credit note" in check(r, "negativeAmounts")["message"]


def test_mixed_currencies():
    r = run("Subtotal: 100 USD", "Tax: 10 EUR", "Total: 110 USD")
    assert r["fields"]["currency"]["status"] == "ambiguous" and check(r, "arithmetic")["status"] == "skipped"


def test_non_invoice_text_yields_missing_fields_only():
    r = run("Dear Sir,", "Thank you for your letter of 2 March 2026.", "Kind regards")
    assert {f["status"] for n, f in r["fields"].items() if n != "vendorName"} == {"missing"}
    assert not inv.is_invoice_like({"category": "letter"}, r)
    assert inv.is_invoice_like({"category": "other"}, run("Invoice No: A-17"))


def test_corrections_are_reparsed_and_originals_kept():
    r = run(*[ln.replace("1,441.00", "1,541.00") for ln in FULL])
    eff = inv.effective(r, {"inv_total": {"status": "CORRECTED", "correctedValue": "$1,441.00", "verifiedBy": "ann"}})
    t = eff["fields"]["total"]
    assert t["status"] == "corrected" and t["value"] == "1441.00" and t["original"]["value"] == "1541.00"
    assert check(eff, "arithmetic")["status"] == "pass"
    assert r["fields"]["total"]["value"] == "1541.00"             # input not mutated
    bad = inv.effective(r, {"inv_total": {"status": "CORRECTED", "correctedValue": "about a thousand"}})
    assert bad["fields"]["total"]["status"] == "invalid"


def test_review_fields_have_no_confidence_and_skip_missing():
    rf = inv.review_fields(run(*NORTHWIND))
    ids = [f["fieldId"] for f in rf]
    assert "inv_total" in ids and "inv_subtotal" not in ids
    assert all(f["confidence"] is None and f["source"] == "deterministic-parser" for f in rf)
    total = next(f for f in rf if f["fieldId"] == "inv_total")
    assert total["value"] == "4,800 USD" and total["evidence"] == "Total Amount Due: 4,800 USD"


def test_empty_and_malformed_input():
    assert {f["status"] for f in inv.extract([])["fields"].values()} == {"missing"}
    assert {f["status"] for f in inv.extract(pages(["", "   "]))["fields"].values()} == {"missing"}
    assert inv.parse_money("no numbers here") is None and inv.parse_date("whenever") is None


def test_currency_review_value_is_the_code():
    rf = {f["fieldId"]: f for f in inv.review_fields(run(*NORTHWIND))}
    assert rf["inv_currency"]["value"] == "USD" and rf["inv_currency"]["parseStatus"] == "inferred"
