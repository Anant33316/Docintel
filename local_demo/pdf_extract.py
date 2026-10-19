"""Validate an uploaded PDF and extract its embedded (selectable) text with PyMuPDF.

This is *text extraction only*: it reads the text layer that the PDF already contains.
It does not perform OCR, layout analysis, key-value (form) detection or table detection,
and it produces no confidence scores. Scanned / image-only PDFs therefore yield no text,
which is reported as an error that names OCR as the missing capability.
"""
import re
import unicodedata

PDF_MAGIC = b"%PDF-"
MAGIC_WINDOW = 1024            # the PDF spec allows the header anywhere in the first 1024 bytes
DEFAULT_MAX_PAGES = 300

try:  # PyMuPDF >= 1.24 exposes "pymupdf"; older releases only expose "fitz"
    import pymupdf as _pymupdf
except ImportError:  # pragma: no cover - depends on the environment
    try:
        import fitz as _pymupdf
    except ImportError:
        _pymupdf = None

if _pymupdf is not None:
    try:  # MuPDF prints repair warnings to stderr for damaged files; we report errors ourselves
        _pymupdf.TOOLS.mupdf_display_errors(False)
    except Exception:  # pragma: no cover
        pass


class PdfError(Exception):
    """A user-facing validation/extraction failure. ``code`` is a stable machine-readable reason."""

    public = True          # message is safe to show to users (processor diagnostics)
    retryable = False      # depends on the file itself: retrying the same bytes gives the same result

    def __init__(self, code, message, http_status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def available():
    return _pymupdf is not None


def engine_version():
    if _pymupdf is None:
        return None
    for attr in ("__version__", "VersionBind"):
        v = getattr(_pymupdf, attr, None)
        if isinstance(v, str) and v:
            return v
    return "unknown"


def looks_like_pdf(data):
    return PDF_MAGIC in data[:MAGIC_WINDOW]


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_line(line):
    line = unicodedata.normalize("NFC", line).replace(" ", " ").replace("�", "")
    line = _CONTROL.sub("", line)
    return re.sub(r"[ \t]+", " ", line).strip()


def extract(data, max_pages=DEFAULT_MAX_PAGES):
    """Return ``{"pages": [[line, ...], ...], "pageCount": n, "charCount": n, "engine": "PyMuPDF x"}``.

    Raises ``PdfError`` for anything that is not a readable PDF. An empty text layer is NOT an
    error here (the caller decides); ``charCount == 0`` signals it.
    """
    if _pymupdf is None:
        raise PdfError("extractor_unavailable",
                       "PyMuPDF is not installed, so PDFs cannot be read locally. "
                       "Run: pip install pymupdf==1.28.2", http_status=503)
    if not data:
        raise PdfError("empty_file", "The file is empty (0 bytes).", http_status=400)
    if not looks_like_pdf(data):
        raise PdfError("not_a_pdf", "The file content is not a PDF (missing the %PDF- header). "
                       "Renaming a file to .pdf does not make it a PDF.", http_status=415)
    try:
        doc = _pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # FileDataError and friends: the bytes are not a parseable PDF
        raise PdfError("malformed_pdf", f"The PDF is damaged or malformed and could not be opened "
                       f"({type(e).__name__}).")
    try:
        if getattr(doc, "needs_pass", False):
            raise PdfError("encrypted_pdf", "The PDF is password-protected. Remove the password and upload again.")
        count = doc.page_count
        if count < 1:
            raise PdfError("malformed_pdf", "The PDF contains no pages (it may be truncated or damaged).")
        if count > max_pages:
            raise PdfError("too_many_pages", f"The PDF has {count} pages; the local demo accepts at most "
                           f"{max_pages}.", http_status=413)
        pages = []
        for i in range(count):
            try:
                raw = doc.load_page(i).get_text("text", sort=True)
            except Exception as e:
                raise PdfError("malformed_pdf", f"Page {i + 1} could not be read ({type(e).__name__}); "
                               "the PDF is probably damaged.")
            lines = [_clean_line(ln) for ln in raw.splitlines()]
            pages.append([ln for ln in lines if ln])
    finally:
        doc.close()
    return {
        "pages": pages,
        "pageCount": len(pages),
        "charCount": sum(len(ln) for p in pages for ln in p),
        "engine": f"PyMuPDF {engine_version()}",
    }
