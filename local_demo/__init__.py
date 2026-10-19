"""Local-demo-only infrastructure for DocIntel.

Nothing in this package is deployed to AWS and nothing under ``src/`` imports it.
It provides honest, offline stand-ins for the AWS services the real handlers call:

* ``pdf_extract``   - validates uploaded PDFs and extracts their embedded text with PyMuPDF
* ``services``      - LocalTextract / LocalBedrock / LocalComprehend / LocalS3 adapters that the
                      unchanged processor and API handlers talk to through ``docintel_common.aws``
* ``heuristics``    - the deterministic, non-AI text heuristics used by those adapters
* ``pipeline``      - the local upload -> S3 -> processor flow, de-duplication and provenance
"""
