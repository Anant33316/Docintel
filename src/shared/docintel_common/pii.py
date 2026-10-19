"""PII detection (Amazon Comprehend DetectPiiEntities) and text redaction."""
from . import aws

PII_LANGS = ("en", "es")             # languages DetectPiiEntities supports
MAX_BYTES = 90_000                   # API limit is 100 KB of UTF-8 per call; stay below it


def split_for_detection(text, max_bytes=MAX_BYTES):
    """Yield ``(offset, piece)`` pieces under ``max_bytes`` UTF-8, cut at newlines when possible."""
    pos = 0
    n = len(text)
    while pos < n:
        end = min(n, pos + max_bytes // 4)          # worst case 4 bytes/char
        # extend greedily while under the byte budget
        while end < n and len(text[pos:min(n, end + 2000)].encode("utf-8")) <= max_bytes:
            end = min(n, end + 2000)
        if end < n:
            cut = text.rfind("\n", pos, end)
            if cut > pos:
                end = cut + 1
        yield pos, text[pos:end]
        pos = end


def detect(text, language="en", min_score=0.0):
    """Return ``(entities, language_used)``; entity offsets are relative to ``text``."""
    lang = language if language in PII_LANGS else "en"
    comprehend = aws.client("comprehend")
    entities = []
    for offset, piece in split_for_detection(text):
        if not piece.strip():
            continue
        r = comprehend.detect_pii_entities(Text=piece, LanguageCode=lang)
        for e in r.get("Entities", []):
            if e["Score"] < min_score:
                continue
            entities.append({"type": e["Type"], "score": round(float(e["Score"]), 4),
                             "beginOffset": e["BeginOffset"] + offset, "endOffset": e["EndOffset"] + offset})
    return entities, lang


def mask_preview(value):
    """'John Smith' -> 'J*** S****' : enough to locate the item, not enough to leak it."""
    out = []
    for word in value.split():
        out.append(word[0] + "*" * (len(word) - 1) if len(word) > 1 else "*")
    return " ".join(out)[:60]


def join_segments(segments):
    """Join segments with newlines and remember each segment's span."""
    segments = [s.replace("\n", " ").replace("\r", " ") for s in segments]   # same length -> offsets stay valid
    spans, pos = [], 0
    for s in segments:
        spans.append((pos, pos + len(s)))
        pos += len(s) + 1
    return "\n".join(segments), spans


def redact_text(text, entities, entity_types=None, min_score=0.5, mask="TYPE"):
    """Replace detected spans. ``mask``: TYPE -> ``[NAME]``, CHAR -> ``*****``."""
    chosen = [e for e in entities
              if e["score"] >= min_score and (not entity_types or e["type"] in entity_types)]
    chosen.sort(key=lambda e: e["beginOffset"])
    out, pos, counts = [], 0, {}
    for e in chosen:
        b, en = e["beginOffset"], e["endOffset"]
        if b < pos:               # overlapping detection: extend the previous redaction only
            if en > pos:
                out.append("" if mask == "TYPE" else "*" * (en - pos))
                pos = en
            continue
        out.append(text[pos:b])
        out.append(f"[{e['type']}]" if mask == "TYPE" else "*" * (en - b))
        counts[e["type"]] = counts.get(e["type"], 0) + 1
        pos = en
    out.append(text[pos:])
    return "".join(out), counts


def redact_ranges(text, ranges, entities, entity_types=None, min_score=0.5, mask="TYPE"):
    """Redact each ``text[start:end]`` range independently.

    Entities overlapping a range are clipped to it, so an entity detected across a
    "key: value" boundary redacts the affected part of both. Each entity is counted once.
    """
    chosen = [e for e in entities
              if e["score"] >= min_score and (not entity_types or e["type"] in entity_types)]
    out, hit = [], set()
    for s, en in ranges:
        local = []
        for idx, e in enumerate(chosen):
            b, f = max(e["beginOffset"], s), min(e["endOffset"], en)
            if b < f:
                local.append({**e, "beginOffset": b - s, "endOffset": f - s})
                hit.add(idx)
        red, _ = redact_text(text[s:en], local, min_score=0.0, mask=mask)
        out.append(red)
    counts = {}
    for idx in hit:
        counts[chosen[idx]["type"]] = counts.get(chosen[idx]["type"], 0) + 1
    return out, counts


def redact_segments(segments, entities, **kw):
    """Redact a list of strings whose detection ran on ``join_segments(segments)``."""
    joined, spans = join_segments(segments)
    return redact_ranges(joined, spans, entities, **kw)
