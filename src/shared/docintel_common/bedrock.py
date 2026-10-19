"""Amazon Bedrock helpers: Converse (chat/LLM) and Titan text embeddings."""
import json
import logging
import os
from array import array
from concurrent.futures import ThreadPoolExecutor

from . import aws

logger = logging.getLogger(__name__)

EMBED_DIMENSIONS = 256          # Titan v2 supports 256/512/1024; 256 keeps DynamoDB items small


def model_id():
    return os.environ["BEDROCK_MODEL_ID"]


def embedding_model_id():
    return os.environ.get("EMBEDDING_MODEL_ID", "").strip()


def converse(system, messages, max_tokens=700, temperature=0.2):
    """Call the Converse API.

    ``messages`` is either a plain user string or a list of
    ``{"role": "user"|"assistant", "text": str}`` turns (must start with user).
    Returns ``(text, usage)`` where usage has inputTokens/outputTokens.
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "text": messages}]
    r = aws.client("bedrock-runtime").converse(
        modelId=model_id(),
        system=[{"text": system}],
        messages=[{"role": m["role"], "content": [{"text": m["text"]}]} for m in messages],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    parts = r["output"]["message"]["content"]
    text = "".join(p.get("text", "") for p in parts)
    usage = r.get("usage") or {}
    return text, {"inputTokens": int(usage.get("inputTokens", 0)), "outputTokens": int(usage.get("outputTokens", 0))}


def parse_json_object(raw):
    """Extract the outermost JSON object from an LLM reply; None if impossible."""
    if not raw or "{" not in raw or "}" not in raw:
        return None
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


# ---------------------------------------------------------------- embeddings
def embed(text):
    """Return a normalized embedding (list of floats) or None when disabled/failing."""
    mid = embedding_model_id()
    if not mid or not text.strip():
        return None
    try:
        r = aws.client("bedrock-runtime").invoke_model(
            modelId=mid,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": text[:20000], "dimensions": EMBED_DIMENSIONS, "normalize": True}),
        )
        return json.loads(r["body"].read())["embedding"]
    except Exception as e:   # embeddings are an optimisation; lexical search still works without them
        logger.warning("embedding failed (%s); continuing without semantic vectors", type(e).__name__)
        return None


def embed_many(texts, workers=4):
    if not embedding_model_id():
        return [None] * len(texts)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(embed, texts))


def pack_vector(vec):
    return array("f", vec).tobytes() if vec else None


def unpack_vector(blob):
    if blob is None:
        return None
    if hasattr(blob, "value"):
        blob = blob.value
    a = array("f")
    a.frombytes(bytes(blob))
    return list(a)
