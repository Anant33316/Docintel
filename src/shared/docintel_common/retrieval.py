"""Deterministic lexical retrieval (BM25) with optional semantic (embedding) re-scoring.

Used by per-document Q&A/chat (rank one document's chunks) and by cross-document
search (rank chunks from all documents). Pure Python, no external services.
"""
import math
import re
from collections import Counter

STOPWORDS = set(
    "a an and are as at be been but by can could did do does for from had has have how i if in into is it its "
    "me my of on or our so than that the their them then there these they this to was we were what when where "
    "which who whom why will with would you your about any all also am"
    .split()
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'\-][a-z0-9]+)*")


def _stem(t):
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def tokenize(text):
    return [_stem(t) for t in _TOKEN.findall((text or "").lower()) if t not in STOPWORDS]


def normalize_ws(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


class BM25:
    def __init__(self, docs_tokens, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(t) for t in docs_tokens]
        self.lens = [len(t) for t in docs_tokens]
        self.avgdl = (sum(self.lens) / len(self.lens)) if self.lens else 0.0
        df = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs_tokens)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query_tokens):
        out = []
        q = set(query_tokens)
        for tf, dl in zip(self.tfs, self.lens):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * (dl / self.avgdl if self.avgdl else 1))
                s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / denom
            out.append(s)
        return out


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else None


def rank(query, items, query_vec=None, text_of=lambda i: i["text"], vec_of=lambda i: i.get("vector"),
         lexical_weight=0.6, semantic_min=0.35):
    """Score ``items`` against ``query``.

    Returns a list (same order as items) of dicts:
    ``{"score": 0..1, "lexical": 0..1, "semantic": float|None, "matchedTerms": [...], "phrase": bool}``.
    An item is a *match* if it has lexical overlap, contains the exact phrase, or (when
    embeddings are available) semantic similarity >= ``semantic_min``.
    """
    q_tokens = tokenize(query)
    toks = [tokenize(text_of(i)) for i in items]
    raw = BM25(toks).scores(q_tokens) if items else []
    top = max(raw) if raw else 0.0
    phrase = normalize_ws(query)
    results = []
    for item, tokens, r in zip(items, toks, raw):
        lex = (r / top) if top > 0 else 0.0
        sem = cosine(query_vec, vec_of(item)) if query_vec is not None else None
        sem_part = max(0.0, sem) if sem is not None else None
        has_phrase = len(phrase) > 3 and phrase in normalize_ws(text_of(item))
        if sem_part is not None:
            score = lexical_weight * lex + (1 - lexical_weight) * sem_part
        else:
            score = lex
        if has_phrase:
            score = min(1.0, score + 0.15)
        matched = sorted(set(q_tokens) & set(tokens))
        results.append({
            "score": round(score, 4),
            "lexical": round(lex, 4),
            "semantic": round(sem, 4) if sem is not None else None,
            "matchedTerms": matched,
            "phrase": has_phrase,
            "isMatch": bool(matched) or has_phrase or (sem is not None and sem >= semantic_min),
        })
    return results


def top_k(query, items, k=5, **kw):
    scored = rank(query, items, **kw)
    order = sorted(range(len(items)), key=lambda i: -scored[i]["score"])
    return [(items[i], scored[i]) for i in order[:k] if scored[i]["isMatch"]]


def snippet(text, terms, width=240):
    """Return a window of ``text`` around the first occurrence of any term."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= width:
        return text
    low = text.lower()
    pos = -1
    for t in terms or []:
        m = re.search(r"\b" + re.escape(t.lower()), low)
        if m and (pos == -1 or m.start() < pos):
            pos = m.start()
    if pos == -1:
        return text[:width].rstrip() + "…"
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
