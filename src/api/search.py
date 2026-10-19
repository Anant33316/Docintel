"""Cross-document search (Feature 5).

Serverless and cheap by design: chunks live in DynamoDB (written by the
processor), the API scans them in parallel and ranks with BM25, optionally
blended with Titan-embedding cosine similarity. No always-on search cluster.

Practical ceiling: ``MAX_SEARCH_CHUNKS`` (default 20,000 chunks ~ a few thousand
documents). Beyond that, move the index to OpenSearch Serverless or a Bedrock
Knowledge Base (see docs/ARCHITECTURE.md).
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from docintel_common import audit, aws, bedrock, retrieval
from docintel_common.ddb import from_ddb, scan_all
from docintel_common.http import ApiError, actor, query, request_id, resp

MAX_SEARCH_CHUNKS = int(os.environ.get("MAX_SEARCH_CHUNKS", "20000"))
SEGMENTS = 4
STATUSES = {"COMPLETED", "FAILED", "PROCESSING"}
DOC_FIELDS = ("docId", "fileName", "status", "category", "createdAt", "completedAt", "summary", "keyPhrases",
              "entities", "language", "pageCount", "reviewStatus")


def _date(value, end=False):
    if not value:
        return None
    try:
        d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApiError(400, "dates must be YYYY-MM-DD")
    return int(d.timestamp()) + (86399 if end else 0)


def _scan_segment(seg):
    attrs = ("docId", "chunkId", "page", "kind", "text", "vector")
    names = {f"#a{i}": a for i, a in enumerate(attrs)}      # placeholders avoid DynamoDB reserved words
    items, _ = scan_all(aws.new_table("chunks"), max_items=MAX_SEARCH_CHUNKS // SEGMENTS + 1, Segment=seg,
                        TotalSegments=SEGMENTS, ProjectionExpression=", ".join(names),
                        ExpressionAttributeNames=names)
    return items


def _load_chunks(doc_ids):
    with ThreadPoolExecutor(max_workers=SEGMENTS) as pool:
        parts = list(pool.map(_scan_segment, range(SEGMENTS)))
    rows = [r for part in parts for r in part if r["docId"] in doc_ids]
    out = []
    for r in rows:
        r = dict(r)
        vec = r.pop("vector", None)
        r = from_ddb(r)
        r["vector"] = bedrock.unpack_vector(vec)
        out.append(r)
    truncated = sum(len(p) for p in parts) >= MAX_SEARCH_CHUNKS
    return out, truncated


def _metadata_text(doc):
    parts = [doc.get("fileName") or "", doc.get("category") or "", doc.get("summary") or ""]
    parts += doc.get("keyPhrases") or []
    parts += [e.get("text", "") for e in doc.get("entities") or [] if isinstance(e, dict)]
    return " \n".join(p for p in parts if p)


def _unique_snippets(hits, terms, limit=3):
    """Top snippets for one document, skipping repeats (overlapping chunks and field chunks often
    contain the same line)."""
    out, seen = [], set()
    for it, s in hits:
        text = retrieval.snippet(it["text"], s["matchedTerms"] or terms)
        key = retrieval.normalize_ws(text)
        if key in seen or any(key in k or k in key for k in seen):
            continue
        seen.add(key)
        out.append({"text": text, "page": it.get("page"), "kind": it.get("kind", "text"), "score": s["score"],
                    "semanticScore": s["semantic"]})
        if len(out) == limit:
            break
    return out


def search(event):
    params = query(event)
    q = (params.get("q") or "").strip()
    if len(q) > 300:
        raise ApiError(400, "q must be at most 300 characters")
    try:
        limit = max(1, min(int(params.get("limit", 10)), 50))
    except ValueError:
        raise ApiError(400, "limit must be an integer")
    category = params.get("category")
    status = params.get("status")
    if status and status not in STATUSES:
        raise ApiError(400, f"status must be one of {sorted(STATUSES)}")
    date_from, date_to = _date(params.get("dateFrom")), _date(params.get("dateTo"), end=True)
    semantic = params.get("semantic", "true").lower() != "false"

    docs, _ = scan_all(aws.table("docs"), max_items=5000,
                       ProjectionExpression=", ".join(f"#{i}" for i in range(len(DOC_FIELDS))),
                       ExpressionAttributeNames={f"#{i}": f for i, f in enumerate(DOC_FIELDS)})
    docs = [from_ddb(d) for d in docs]
    docs = [d for d in docs
            if (not category or d.get("category") == category)
            and (not status or d.get("status") == status)
            and (date_from is None or d.get("createdAt", 0) >= date_from)
            and (date_to is None or d.get("createdAt", 0) <= date_to)]
    filters = {"category": category, "status": status, "dateFrom": params.get("dateFrom"),
               "dateTo": params.get("dateTo")}

    if not q:   # filter-only browse
        docs.sort(key=lambda d: d.get("createdAt", 0), reverse=True)
        out = [{k: d.get(k) for k in ("docId", "fileName", "category", "status", "createdAt")} for d in docs[:limit]]
        return resp(200, {"query": "", "filters": filters, "total": len(docs), "results": out, "searchMode": "browse"})

    by_id = {d["docId"]: d for d in docs}
    chunks, truncated = _load_chunks(set(by_id))
    pool = chunks + [{"docId": d["docId"], "chunkId": "meta", "kind": "metadata", "page": None,
                      "text": _metadata_text(d)} for d in docs]
    qvec = bedrock.embed(q) if semantic and any(c.get("vector") for c in chunks) else None
    scores = retrieval.rank(q, pool, query_vec=qvec)

    per_doc = {}
    for item, sc in zip(pool, scores):
        if sc["isMatch"]:
            per_doc.setdefault(item["docId"], []).append((item, sc))

    results = []
    for doc_id, hits in per_doc.items():
        hits.sort(key=lambda h: -h[1]["score"])
        best = hits[0][1]["score"]
        score = min(1.0, best + 0.05 * min(len(hits) - 1, 4))     # small boost for repeated evidence
        d = by_id[doc_id]
        terms = sorted({t for _, s in hits for t in s["matchedTerms"]})
        results.append({
            "docId": doc_id,
            "fileName": d.get("fileName"),
            "category": d.get("category"),
            "status": d.get("status"),
            "createdAt": d.get("createdAt"),
            "score": round(score, 4),
            "matchedTerms": terms,
            "matchCount": len(hits),
            "snippets": _unique_snippets(hits, terms),
        })
    results.sort(key=lambda r: (-r["score"], -(r.get("createdAt") or 0)))
    mode = "hybrid" if qvec else "lexical"
    audit.record("DOCUMENTS_SEARCHED", None, actor(event),
                 {"resultCount": len(results), "mode": mode, "queryLength": len(q)},
                 request_id=request_id(event))
    return resp(200, {"query": q, "filters": filters, "searchMode": mode, "total": len(results),
                      "results": results[:limit], "scannedChunks": len(chunks), "indexTruncated": truncated})
