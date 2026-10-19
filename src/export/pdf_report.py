"""Minimal, dependency-free PDF writer for human-readable reports.

Uses the PDF base-14 Helvetica fonts (no font embedding), WinAnsi encoding and
Flate-compressed content streams. Characters outside Windows-1252 are replaced
with '?', which is acceptable for a summary report (the JSON/CSV exports keep
the full Unicode text).
"""
import zlib

PAGE_W, PAGE_H = 595.28, 841.89      # A4 in points
MARGIN = 50

# Helvetica advance widths (1/1000 em) for ASCII 32..126
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
      556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
      1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
      667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
      333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
      556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def text_width(s, size, bold=False):
    w = sum(_W[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in s)
    return w * size / 1000 * (1.07 if bold else 1.0)


def _clean(s):
    s = str(s).replace("\t", "    ")
    return s.encode("cp1252", errors="replace").decode("cp1252")


def _escape(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def wrap(text, size, width, bold=False):
    out = []
    for para in _clean(text).split("\n"):
        line = ""
        for word in para.split(" "):
            candidate = f"{line} {word}" if line else word
            if text_width(candidate, size, bold) <= width:
                line = candidate
                continue
            if line:
                out.append(line)
            while text_width(word, size, bold) > width:      # hard-break very long tokens
                cut = len(word)
                while cut > 1 and text_width(word[:cut], size, bold) > width:
                    cut -= 1
                out.append(word[:cut])
                word = word[cut:]
            line = word
        out.append(line)
    return out


class PdfReport:
    def __init__(self, title):
        self.title = _clean(title)
        self.pages = []
        self._new_page()

    # ---- layout primitives
    def _new_page(self):
        self.ops = []
        self.pages.append(self.ops)
        self.y = PAGE_H - MARGIN

    def _ensure(self, h):
        if self.y - h < MARGIN + 20:
            self._new_page()

    def _line(self, s, size, bold=False, x=MARGIN, gray=0.0):
        font = "F2" if bold else "F1"
        self.ops.append(f"BT {gray:.2f} g /{font} {size} Tf {x:.2f} {self.y:.2f} Td ({_escape(s)}) Tj ET")

    def text(self, s, size=10, bold=False, indent=0, gray=0.0, leading=1.35):
        width = PAGE_W - 2 * MARGIN - indent
        for ln in wrap(s, size, width, bold):
            self._ensure(size * leading)
            self.y -= size * leading
            self._line(ln, size, bold, MARGIN + indent, gray)

    def heading(self, s, level=1):
        size = {0: 18, 1: 13, 2: 11}[level]
        self._ensure(size * 3)
        self.y -= size * 0.8
        self.text(s, size=size, bold=True)
        if level <= 1:
            self.y -= 4
            self.ops.append(f"0.75 G 0.6 w {MARGIN} {self.y:.2f} m {PAGE_W - MARGIN} {self.y:.2f} l S")
        self.y -= 4

    def kv(self, key, value, size=10):
        self.text(f"{key}: {value}", size=size)

    def bullet(self, s, size=10):
        width = PAGE_W - 2 * MARGIN - 14
        lines = wrap(s, size, width)
        for i, ln in enumerate(lines):
            self._ensure(size * 1.35)
            self.y -= size * 1.35
            if i == 0:
                self._line("-", size, x=MARGIN + 4)
            self._line(ln, size, x=MARGIN + 14)

    def space(self, h=6):
        self.y -= h

    # ---- serialisation
    def build(self):
        total = len(self.pages)
        for n, ops in enumerate(self.pages, start=1):
            footer = _escape(f"{self.title}  -  page {n} of {total}")
            ops.append(f"BT 0.45 g /F1 8 Tf {MARGIN} {MARGIN - 20} Td ({footer}) Tj ET")

        objects = []

        def add(obj):
            objects.append(obj)
            return len(objects)

        catalog = add(None)
        pages_id = add(None)
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        for ops in self.pages:
            data = zlib.compress("\n".join(ops).encode("cp1252", errors="replace"))
            content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
            page = add((f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                        f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {content} 0 R >>").encode())
            kids.append(page)
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        objects[pages_id - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                                 f"/Count {len(kids)} >>").encode()
        info = add(f"<< /Title ({_escape(self.title)}) /Producer (DocIntel) >>".encode("cp1252", errors="replace"))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)
