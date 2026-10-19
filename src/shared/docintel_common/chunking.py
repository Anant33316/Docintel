"""Split extracted text into page-aware chunks used for retrieval and citations.

Chunks never cross a page boundary, so every chunk carries one page number that
can be cited in answers.
"""


def build_chunks(page_lines, max_chars=1200, overlap_lines=1):
    """``page_lines``: ``[{"page": 1, "lines": [{"text": str, "confidence": float|None}]}]``."""
    chunks = []
    for page in page_lines:
        lines = [ln for ln in page.get("lines", []) if (ln.get("text") or "").strip()]
        i = 0
        while i < len(lines):
            size, j = 0, i
            while j < len(lines) and (size == 0 or size + len(lines[j]["text"]) + 1 <= max_chars):
                size += len(lines[j]["text"]) + 1
                j += 1
            part = lines[i:j]
            confs = [ln["confidence"] for ln in part if ln.get("confidence") is not None]
            chunks.append({
                "chunkId": f"c{len(chunks) + 1:04d}",
                "page": page.get("page"),
                "text": "\n".join(ln["text"] for ln in part)[: max_chars * 2],
                "ocrConfidence": round(sum(confs) / len(confs), 2) if confs else None,
            })
            if j >= len(lines):
                break
            i = max(j - overlap_lines, i + 1)
    return chunks


def page_lines_from_text(text):
    """Legacy results (processed before the upgrade) have no page info: treat as one unknown page."""
    return [{"page": None, "lines": [{"text": t, "confidence": None} for t in text.split("\n")]}]


def line_offsets(page_lines):
    """Map character offsets in ``"\\n".join(all lines)`` back to pages.

    Returns a list of ``(start_offset, end_offset, page)`` per line, in order.
    """
    out, pos = [], 0
    for page in page_lines:
        for ln in page.get("lines", []):
            n = len(ln.get("text") or "")
            out.append((pos, pos + n, page.get("page")))
            pos += n + 1
    return out


def page_for_offset(offsets, offset):
    lo, hi = 0, len(offsets) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        s, e, p = offsets[mid]
        if offset < s:
            hi = mid - 1
        elif offset > e:
            lo = mid + 1
        else:
            return p
    return None
