"""Deterministic (non-AI) document comparison: line diff + extracted-field diff.

Everything produced here is a verifiable fact about the two extracted texts; it
is kept strictly separate from the LLM interpretation in the API response.
"""
import difflib
import re

MAX_ITEMS = 100
MAX_LINE = 300
PAIR_MIN_SIMILARITY = 0.6
MAX_PAIRING_BLOCK = 200      # avoid O(n*m) blow-ups on huge replace blocks


def _norm_line(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _norm_key(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _norm_val(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def diff_lines(text_a, text_b):
    a = [_norm_line(x) for x in (text_a or "").split("\n") if _norm_line(x)]
    b = [_norm_line(x) for x in (text_b or "").split("\n") if _norm_line(x)]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added, removed, changed = [], [], []
    unchanged = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            unchanged += i2 - i1
        elif op == "insert":
            added.extend(b[j1:j2])
        elif op == "delete":
            removed.extend(a[i1:i2])
        else:  # replace: a line counts as "changed" only if it closely resembles a line on the other side
            free = list(range(j1, j2))
            if (i2 - i1) * (j2 - j1) > MAX_PAIRING_BLOCK ** 2:
                removed.extend(a[i1:i2])
                added.extend(b[j1:j2])
                continue
            for i in range(i1, i2):
                best, best_ratio = None, PAIR_MIN_SIMILARITY
                for j in free:
                    ratio = difflib.SequenceMatcher(None, a[i], b[j]).ratio()
                    if ratio >= best_ratio:
                        best, best_ratio = j, ratio
                if best is None:
                    removed.append(a[i])
                else:
                    free.remove(best)
                    changed.append({"before": a[i], "after": b[best], "similarity": round(best_ratio, 3)})
            added.extend(b[j] for j in free)

    tok_a = set(re.findall(r"\w+", (text_a or "").lower()))
    tok_b = set(re.findall(r"\w+", (text_b or "").lower()))
    jaccard = len(tok_a & tok_b) / len(tok_a | tok_b) if (tok_a | tok_b) else 1.0

    def cap(lines):
        return [ln[:MAX_LINE] for ln in lines[:MAX_ITEMS]]

    return {
        "lineCountA": len(a),
        "lineCountB": len(b),
        "unchangedLines": unchanged,
        "addedCount": len(added),
        "removedCount": len(removed),
        "changedCount": len(changed),
        "added": cap(added),
        "removed": cap(removed),
        "changed": [{"before": c["before"][:MAX_LINE], "after": c["after"][:MAX_LINE], "similarity": c["similarity"]}
                    for c in changed[:MAX_ITEMS]],
        "lineSimilarity": round(sm.ratio(), 4),
        "vocabularyOverlap": round(jaccard, 4),
        "truncated": max(len(added), len(removed), len(changed)) > MAX_ITEMS,
    }


def diff_fields(fields_a, fields_b):
    """``fields_*``: dict key -> value (the extracted form key/values)."""
    na = {_norm_key(k): (k, v) for k, v in (fields_a or {}).items() if _norm_key(k)}
    nb = {_norm_key(k): (k, v) for k, v in (fields_b or {}).items() if _norm_key(k)}
    added = [{"key": nb[k][0], "value": nb[k][1]} for k in nb if k not in na]
    removed = [{"key": na[k][0], "value": na[k][1]} for k in na if k not in nb]
    changed, same = [], []
    for k in na:
        if k in nb:
            if _norm_val(na[k][1]) != _norm_val(nb[k][1]):
                changed.append({"key": na[k][0], "before": na[k][1], "after": nb[k][1]})
            else:
                same.append({"key": na[k][0], "value": na[k][1]})
    return {"added": added[:MAX_ITEMS], "removed": removed[:MAX_ITEMS], "changed": changed[:MAX_ITEMS],
            "unchanged": same[:MAX_ITEMS]}


def compare(result_a, result_b):
    return {
        "method": "deterministic",
        "description": "Exact line-level and extracted-field differences computed with difflib; no AI involved.",
        "text": diff_lines(result_a.get("text", ""), result_b.get("text", "")),
        "fields": diff_fields(result_a.get("keyValues", {}), result_b.get("keyValues", {})),
    }
