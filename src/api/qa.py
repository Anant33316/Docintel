"""Source-grounded Q&A (Feature 9) and per-document chat with history (Feature 2).

Pipeline per question:
  1. retrieve the most relevant chunks of *this* document (BM25 + optional embeddings)
  2. give the model only those numbered passages (+ the last few chat turns)
  3. validate the model's citations/quotes against the passages (grounding.interpret)
  4. persist the turn (chat only) and write a content-free audit event
"""
import os
import uuid

from docintel_common import audit, aws, bedrock, grounding, results, retrieval
from docintel_common.aws import ClientError, error_code
from docintel_common.chunking import build_chunks
from docintel_common.ddb import from_ddb, now_iso, now_ms, query_all, to_ddb, ttl_after_days
from docintel_common.http import ApiError, actor, body_of, path_param, request_id, resp

MAX_Q = 1000
TOP_K = int(os.environ.get("QA_TOP_K", "6"))
MAX_CONTEXT_CHARS = 12000
HISTORY_MESSAGES = 6            # last 3 exchanges are sent back to the model
FALLBACK_CHUNKS = 4


# ---------------------------------------------------------------- helpers
def load_completed(doc_id):
    item = aws.table("docs").get_item(Key={"docId": doc_id}).get("Item")
    if not item:
        raise ApiError(404, "document not found")
    if item.get("status") != "COMPLETED":
        raise ApiError(409, f"document is {item.get('status')}, try again shortly")
    return from_ddb(item)


def load_chunks(item):
    """Chunks from the Chunks table; documents processed before the upgrade are chunked on the fly."""
    rows = query_all(aws.table("chunks"), max_items=3000, KeyConditionExpression="docId = :d",
                     ExpressionAttributeValues={":d": item["docId"]})
    if rows:
        chunks = []
        for r in rows:
            r = dict(r)
            vec = r.pop("vector", None)
            r = from_ddb(r)
            r["vector"] = bedrock.unpack_vector(vec)
            chunks.append(r)
        return sorted(chunks, key=lambda c: c["chunkId"])
    data = results.load(item)
    chunks = build_chunks(data["pageLines"])
    for f in data["fields"]:
        chunks.append({"chunkId": f"k{f['fieldId']}", "kind": "field", "page": f.get("page"),
                       "text": f"{f['key']}: {f['value']}", "ocrConfidence": f.get("confidence")})
    return chunks


def retrieve(item, query_text):
    chunks = load_chunks(item)
    use_vectors = any(c.get("vector") for c in chunks)
    qvec = bedrock.embed(query_text) if use_vectors else None
    ranked = retrieval.top_k(query_text, chunks, k=TOP_K, query_vec=qvec)
    mode = "hybrid" if qvec else "lexical"
    if not ranked:
        # nothing matched lexically/semantically: show the start of the document instead of
        # refusing outright (handles paraphrased questions when embeddings are disabled)
        text_chunks = [c for c in chunks if c.get("kind", "text") == "text"][:FALLBACK_CHUNKS]
        ranked = [(c, {"score": 0.0, "lexical": 0.0, "semantic": None, "matchedTerms": []}) for c in text_chunks]
        mode = "fallback-first-pages"
    kept, used = [], 0
    for c, info in ranked:
        if used + len(c["text"]) > MAX_CONTEXT_CHARS and kept:
            break
        kept.append((c, info))
        used += len(c["text"])
    return grounding.build_sources(kept, item["docId"], item.get("fileName")), mode


def answer_question(item, question, history=None):
    """Return (result_dict, usage). ``history``: prior turns ``[{"role", "text"}]``."""
    history = history or []
    last_user = next((m["text"] for m in reversed(history) if m["role"] == "user"), "")
    # follow-ups ("what about the second one?") retrieve better with the previous question attached
    sources, mode = retrieve(item, f"{question} {last_user}".strip())

    turns = []
    for m in history[-HISTORY_MESSAGES:]:
        if turns and turns[-1]["role"] == m["role"]:
            continue                                   # keep strict user/assistant alternation
        if not turns and m["role"] != "user":
            continue
        turns.append({"role": m["role"], "text": m["text"][:1500]})
    if turns and turns[-1]["role"] == "user":
        turns.pop()
    turns.append({"role": "user", "text": grounding.build_user_turn(question, sources)})

    raw, usage = bedrock.converse(grounding.SYSTEM_PROMPT, turns, max_tokens=700, temperature=0.1)
    result = grounding.interpret(raw, sources)
    # How the answer was produced. Bedrock replies never set "engine"; the offline stand-in does
    # (template / extractive / none), so the UI can say that no language model was involved.
    engine = (bedrock.parse_json_object(raw) or {}).get("engine")
    result["answerMode"] = engine if engine in ("template", "extractive", "none") else "llm"
    result["retrieval"] = {
        "mode": mode,
        "sources": [{k: s[k] for k in ("id", "page", "kind", "relevance", "lexicalScore", "semanticScore",
                                        "ocrConfidence", "chunkId")} for s in sources],
    }
    return result, usage


# ---------------------------------------------------------------- POST /documents/{docId}/ask
def ask(event):
    doc_id = path_param(event, "docId")
    body = body_of(event)
    question = (body.get("question") or "").strip()
    if not question or len(question) > MAX_Q:
        return resp(400, {"error": f"question required (max {MAX_Q} chars)"})
    item = load_completed(doc_id)
    result, usage = answer_question(item, question)
    audit.record("DOCUMENT_QUERIED", doc_id, actor(event, body),
                 {"mode": "ask", "answerable": result["answerable"], "citations": len(result["citations"]),
                  "confidence": result["confidence"]},
                 counters={"bedrockInputTokens": usage["inputTokens"], "bedrockOutputTokens": usage["outputTokens"]},
                 request_id=request_id(event))
    # "question" and "answer" keep the original response contract
    return resp(200, {"question": question, "docId": doc_id, **result})


# ---------------------------------------------------------------- chat
def _session_key(doc_id, session_id):
    return {"pk": f"DOC#{doc_id}", "sk": f"SESSION#{session_id}"}


def _history(session_id, limit=HISTORY_MESSAGES):
    r = aws.table("app").query(
        KeyConditionExpression="pk = :p AND begins_with(sk, :m)",
        ExpressionAttributeValues={":p": f"CHAT#{session_id}", ":m": "MSG#"},
        ScanIndexForward=False, Limit=limit,
    )
    return [from_ddb(m) for m in reversed(r.get("Items", []))]


def _get_session(doc_id, session_id, user):
    s = aws.table("app").get_item(Key=_session_key(doc_id, session_id)).get("Item")
    if not s:
        raise ApiError(404, "chat session not found")
    if s.get("userId") != user:
        raise ApiError(403, "chat session belongs to a different user")
    return from_ddb(s)


def chat(event):
    doc_id = path_param(event, "docId")
    body = body_of(event)
    user = actor(event, body)
    question = (body.get("question") or "").strip()
    if not question or len(question) > MAX_Q:
        raise ApiError(400, f"question required (max {MAX_Q} chars)")
    session_id = body.get("sessionId") or uuid.uuid4().hex
    if not isinstance(session_id, str) or not session_id.isalnum() or len(session_id) > 64:
        raise ApiError(400, "invalid sessionId")

    item = load_completed(doc_id)
    history = []
    if body.get("sessionId"):
        _get_session(doc_id, session_id, user)
        history = _history(session_id)

    result, usage = answer_question(item, question, history)

    ttl = ttl_after_days(os.environ.get("CHAT_HISTORY_TTL_DAYS", "30"))
    ts, iso = now_ms(), now_iso()
    turn = f"{ts:013d}#{uuid.uuid4().hex[:8]}"        # unique even if two turns land in the same millisecond
    table = aws.table("app")
    try:
        # creates the session on first use; the condition enforces ownership atomically
        table.update_item(
            Key=_session_key(doc_id, session_id),
            UpdateExpression=("SET userId = if_not_exists(userId, :u), docId = :d, sessionId = :sid, "
                              "title = if_not_exists(title, :title), createdAt = if_not_exists(createdAt, :now), "
                              "updatedAt = :now, expiresAt = :ttl, #t = :type ADD messageCount :two"),
            ConditionExpression="attribute_not_exists(pk) OR userId = :u",
            ExpressionAttributeNames={"#t": "type"},
            ExpressionAttributeValues={":u": user, ":d": doc_id, ":sid": session_id, ":title": question[:80],
                                       ":now": iso, ":ttl": ttl, ":two": 2, ":type": "CHAT_SESSION"},
        )
    except ClientError as e:
        if error_code(e) == "ConditionalCheckFailedException":
            raise ApiError(403, "chat session belongs to a different user")
        raise
    with table.batch_writer() as batch:
        batch.put_item(Item={"pk": f"CHAT#{session_id}", "sk": f"MSG#{turn}#0", "role": "user",
                             "text": question, "createdAt": iso, "expiresAt": ttl})
        batch.put_item(Item=to_ddb({
            "pk": f"CHAT#{session_id}", "sk": f"MSG#{turn}#1", "role": "assistant", "text": result["answer"],
            "createdAt": iso, "expiresAt": ttl, "answerable": result["answerable"],
            "confidence": result["confidence"], "citations": result["citations"], "warnings": result["warnings"],
        }))
    audit.record("DOCUMENT_QUERIED", doc_id, user,
                 {"mode": "chat", "sessionId": session_id, "answerable": result["answerable"],
                  "citations": len(result["citations"]), "confidence": result["confidence"],
                  "historyTurns": len(history)},
                 counters={"bedrockInputTokens": usage["inputTokens"], "bedrockOutputTokens": usage["outputTokens"]},
                 request_id=request_id(event))
    return resp(200, {"docId": doc_id, "sessionId": session_id, "question": question, **result})


def list_sessions(event):
    doc_id = path_param(event, "docId")
    user = actor(event)
    rows = query_all(aws.table("app"), max_items=200, KeyConditionExpression="pk = :p AND begins_with(sk, :s)",
                     ExpressionAttributeValues={":p": f"DOC#{doc_id}", ":s": "SESSION#"})
    sessions = [from_ddb({k: r.get(k) for k in ("sessionId", "title", "createdAt", "updatedAt", "messageCount")})
                for r in rows if r.get("userId") == user]
    sessions.sort(key=lambda s: s.get("updatedAt") or "", reverse=True)
    return resp(200, {"docId": doc_id, "userId": user, "sessions": sessions})


def get_session(event):
    doc_id = path_param(event, "docId")
    session_id = path_param(event, "sessionId")
    session = _get_session(doc_id, session_id, actor(event))
    msgs = query_all(aws.table("app"), max_items=500, KeyConditionExpression="pk = :p AND begins_with(sk, :m)",
                     ExpressionAttributeValues={":p": f"CHAT#{session_id}", ":m": "MSG#"})
    messages = [from_ddb({k: v for k, v in m.items() if k not in ("pk", "sk", "expiresAt")}) for m in msgs]
    return resp(200, {"docId": doc_id, "sessionId": session_id, "title": session.get("title"),
                      "createdAt": session.get("createdAt"), "messages": messages})


def delete_session(event):
    doc_id = path_param(event, "docId")
    session_id = path_param(event, "sessionId")
    user = actor(event)
    _get_session(doc_id, session_id, user)
    table = aws.table("app")
    msgs = query_all(table, max_items=5000, KeyConditionExpression="pk = :p",
                     ExpressionAttributeValues={":p": f"CHAT#{session_id}"}, ProjectionExpression="pk, sk")
    with table.batch_writer() as batch:
        for m in msgs:
            batch.delete_item(Key={"pk": m["pk"], "sk": m["sk"]})
        batch.delete_item(Key=_session_key(doc_id, session_id))
    audit.record("CHAT_SESSION_DELETED", doc_id, user, {"sessionId": session_id, "messages": len(msgs)},
                 request_id=request_id(event))
    return resp(200, {"deleted": True, "sessionId": session_id, "messagesDeleted": len(msgs)})
