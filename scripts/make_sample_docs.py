#!/usr/bin/env python3
"""Generate two small, related sample PDFs for demos and live integration tests.

    python scripts/make_sample_docs.py [out_dir]     -> sample_invoice_v1.pdf, sample_invoice_v2.pdf

Uses the project's own dependency-free PDF writer (src/export/pdf_report.py).
The data is fictional.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "export"))
from pdf_report import PdfReport  # noqa: E402

V1 = [
    ("Northwind Cloud Consulting - Invoice", None),
    ("Invoice Number: NW-2026-001", None),
    ("Invoice Date: September 15, 2026", None),
    ("Bill To: Priya Sharma, 42 Market Street, Springfield", None),
    ("Email: priya.sharma@example.com   Phone: 555-201-3344", None),
    ("Account Number: 00123456789", None),
    ("Services: Serverless migration of the reporting pipeline to AWS Lambda and Amazon SQS.", None),
    ("Total Amount Due: 4,800 USD", None),
    ("Payment Terms: Net 30. Payment is due by October 15, 2026.", None),
    ("Submit the report before October 20, 2026.", None),
    ("The support contract renews on January 1, 2027 unless cancelled in writing.", None),
]
V2 = [
    ("Northwind Cloud Consulting - Invoice", None),
    ("Invoice Number: NW-2026-001-R1", None),
    ("Invoice Date: September 22, 2026", None),
    ("Bill To: Priya Sharma, 42 Market Street, Springfield", None),
    ("Email: priya.sharma@example.com   Phone: 555-201-3344", None),
    ("Account Number: 00123456789", None),
    ("Services: Serverless migration of the reporting pipeline to AWS Lambda, Amazon SQS and Amazon DynamoDB.", None),
    ("Total Amount Due: 5,350 USD", None),
    ("Payment Terms: Due on receipt.", None),
    ("A late fee of 2% per month applies after October 15, 2026.", None),
    ("Submit the report before October 20, 2026.", None),
]


def write(lines, path, title):
    pdf = PdfReport(title)
    pdf.heading(lines[0][0], level=0)
    for text, _ in lines[1:]:
        pdf.text(text, size=11)
        pdf.space(4)
    with open(path, "wb") as f:
        f.write(pdf.build())
    return path


def main(out_dir="."):
    os.makedirs(out_dir, exist_ok=True)
    a = write(V1, os.path.join(out_dir, "sample_invoice_v1.pdf"), "Sample invoice v1")
    b = write(V2, os.path.join(out_dir, "sample_invoice_v2.pdf"), "Sample invoice v2")
    print(a)
    print(b)
    return a, b


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
