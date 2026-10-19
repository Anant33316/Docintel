"""In-memory fakes for the AWS APIs the project uses.

They are intentionally strict where real AWS is strict, so tests catch real bugs:
  * DynamoDB rejects Python floats, unknown expression placeholders, reserved
    words used without a ``#name`` placeholder, and SET on a nested path whose
    parent map does not exist (ValidationException) - exactly like DynamoDB.
  * Numbers come back as ``Decimal`` like the boto3 resource API.
  * Condition / key-condition / update expressions are parsed, not pattern-matched.
"""
import copy
import hashlib
import io
import json
import math
import re
from decimal import Decimal

from docintel_common.aws import ClientError

RESERVED = {
    "action", "add", "all", "and", "between", "by", "comment", "count", "data", "date", "day", "delete", "desc",
    "fields", "file", "filter", "format", "from", "group", "hash", "hour", "in", "index", "item", "items", "key",
    "keys", "language", "level", "limit", "list", "map", "max", "min", "minute", "mode", "month", "name", "names",
    "not", "null", "number", "or", "order", "owner", "path", "plan", "range", "region", "result", "role", "second",
    "segment", "session", "set", "size", "source", "state", "status", "string", "sum", "table", "text", "time",
    "timestamp", "to", "token", "total", "ttl", "type", "url", "user", "users", "uuid", "value", "values", "view",
    "year", "zone", "public", "hidden", "percent", "rank", "raw", "comment", "object", "connection", "condition",
}


def client_error(code, op, msg=""):
    return ClientError({"Error": {"Code": code, "Message": msg or code}}, op)


def _check_types(v, path="item"):
    if isinstance(v, float):
        raise TypeError(f"Float types are not supported. Use Decimal types instead ({path})")
    if isinstance(v, dict):
        for k, x in v.items():
            _check_types(x, f"{path}.{k}")
    elif isinstance(v, (list, tuple)):
        for i, x in enumerate(v):
            _check_types(x, f"{path}[{i}]")


def _to_resource(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, dict):
        return {k: _to_resource(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_resource(x) for x in v]
    return v


# ---------------------------------------------------------------- expression parsing
_TOK = re.compile(r"\s*(?:(#[A-Za-z0-9_]+)|(:[A-Za-z0-9_]+)|([A-Za-z_][A-Za-z0-9_]*)|(<>|<=|>=|=|<|>)|([(),.+\-]))")


def _tokens(expr):
    pos, out = 0, []
    expr = expr.strip()
    while pos < len(expr):
        m = _TOK.match(expr, pos)
        if not m or m.end() == pos:
            raise client_error("ValidationException", "Expression", f"bad token at {expr[pos:]!r}")
        name, val, ident, op, punct = m.groups()
        if name:
            out.append(("NAME", name))
        elif val:
            out.append(("VAL", val))
        elif ident:
            out.append(("ID", ident))
        elif op:
            out.append(("OP", op))
        else:
            out.append(("P", punct))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, expr, names, values, used):
        self.t = _tokens(expr)
        self.i = 0
        self.names = names or {}
        self.values = values or {}
        self.used = used

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else (None, None)

    def take(self, kind=None, val=None):
        tok = self.peek()
        if (kind and tok[0] != kind) or (val and (tok[1] or "").upper() != val.upper()):
            raise client_error("ValidationException", "Expression", f"expected {kind} {val}, got {tok}")
        self.i += 1
        return tok

    def kw(self, word):
        tok = self.peek()
        return tok[0] == "ID" and tok[1].upper() == word

    # path := seg ('.' seg)*
    def path(self):
        segs = [self.seg()]
        while self.peek() == ("P", "."):
            self.take()
            segs.append(self.seg())
        return ("path", segs)

    def seg(self):
        kind, v = self.take()
        if kind == "NAME":
            if v not in self.names:
                raise client_error("ValidationException", "Expression", f"undefined name {v}")
            self.used.add(v)
            return self.names[v]
        if kind == "ID":
            if v.lower() in RESERVED:
                raise client_error("ValidationException", "Expression",
                                   f"Attribute name is a reserved keyword; reserved keyword: {v}")
            return v
        raise client_error("ValidationException", "Expression", f"bad path token {v}")

    def operand(self):
        kind, v = self.peek()
        if kind == "VAL":
            self.take()
            if v not in self.values:
                raise client_error("ValidationException", "Expression", f"undefined value {v}")
            self.used.add(v)
            return ("val", self.values[v])
        if kind == "ID" and self.peek(1) == ("P", "(") and v in ("if_not_exists", "list_append", "size"):
            self.take()
            self.take("P", "(")
            a = self.operand()
            args = [a]
            while self.peek() == ("P", ","):
                self.take()
                args.append(self.operand())
            self.take("P", ")")
            return ("fn", v, args)
        return self.path()

    # ---- conditions
    def cond(self):
        left = self.cand()
        while self.kw("OR"):
            self.take()
            left = ("or", left, self.cand())
        return left

    def cand(self):
        left = self.cnot()
        while self.kw("AND"):
            self.take()
            left = ("and", left, self.cnot())
        return left

    def cnot(self):
        if self.kw("NOT"):
            self.take()
            return ("not", self.cnot())
        return self.cprimary()

    def cprimary(self):
        if self.peek() == ("P", "("):
            self.take()
            e = self.cond()
            self.take("P", ")")
            return e
        kind, v = self.peek()
        if kind == "ID" and v in ("attribute_exists", "attribute_not_exists", "begins_with", "contains"):
            self.take()
            self.take("P", "(")
            args = [self.operand()]
            while self.peek() == ("P", ","):
                self.take()
                args.append(self.operand())
            self.take("P", ")")
            return ("call", v, args)
        left = self.operand()
        _, op = self.take("OP")
        return ("cmp", op, left, self.operand())

    # ---- updates
    def update(self):
        actions = []
        while self.peek()[0]:
            _, word = self.take("ID")
            word = word.upper()
            while True:
                if word == "SET":
                    p = self.path()
                    self.take("OP", "=")
                    v = self.operand()
                    if self.peek() in (("P", "+"), ("P", "-")):
                        _, sign = self.take()
                        v = ("arith", sign, v, self.operand())
                    actions.append(("SET", p, v))
                elif word == "REMOVE":
                    actions.append(("REMOVE", self.path()))
                elif word == "ADD":
                    p = self.path()
                    actions.append(("ADD", p, self.operand()))
                else:
                    raise client_error("ValidationException", "UpdateItem", f"unsupported clause {word}")
                if self.peek() == ("P", ","):
                    self.take()
                    continue
                break
        return actions


_MISSING = object()


def _get(item, segs):
    cur = item
    for s in segs:
        if not isinstance(cur, dict) or s not in cur:
            return _MISSING
        cur = cur[s]
    return cur


def _eval(node, item):
    kind = node[0]
    if kind == "val":
        return node[1]
    if kind == "path":
        return _get(item, node[1])
    if kind == "fn":
        name, args = node[1], node[2]
        if name == "if_not_exists":
            cur = _eval(args[0], item)
            return _eval(args[1], item) if cur is _MISSING else cur
        if name == "list_append":
            return list(_eval(args[0], item)) + list(_eval(args[1], item))
        if name == "size":
            return len(_eval(args[0], item))
    if kind == "arith":
        a, b = _eval(node[2], item), _eval(node[3], item)
        return a + b if node[1] == "+" else a - b
    raise ValueError(node)


def _num(v):
    return Decimal(str(v)) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else v


def _test(node, item):
    kind = node[0]
    if kind == "or":
        return _test(node[1], item) or _test(node[2], item)
    if kind == "and":
        return _test(node[1], item) and _test(node[2], item)
    if kind == "not":
        return not _test(node[1], item)
    if kind == "call":
        name, args = node[1], node[2]
        v = _eval(args[0], item)
        if name == "attribute_exists":
            return v is not _MISSING
        if name == "attribute_not_exists":
            return v is _MISSING
        if name == "begins_with":
            return isinstance(v, str) and v.startswith(_eval(args[1], item))
        if name == "contains":
            return v is not _MISSING and _eval(args[1], item) in v
    if kind == "cmp":
        a, b = _eval(node[2], item), _eval(node[3], item)
        if a is _MISSING or b is _MISSING:
            return node[1] == "<>"
        a, b = _num(a), _num(b)
        try:
            return {"=": a == b, "<>": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[node[1]]
        except TypeError:
            return False
    raise ValueError(node)


def _check_unused(names, values, used, op):
    unused = [k for k in list((names or {}).keys()) + list((values or {}).keys()) if k not in used]
    if unused:
        raise client_error("ValidationException", op,
                           f"Value provided in ExpressionAttributeNames/Values unused in expressions: {unused}")


class FakeTable:
    def __init__(self, name, hash_key, range_key=None, indexes=None):
        self.name = name
        self.hk, self.rk = hash_key, range_key
        self.indexes = indexes or {}
        self.items = {}
        self.calls = []

    # ---- helpers
    def _key(self, d):
        try:
            return (d[self.hk], d[self.rk]) if self.rk else (d[self.hk],)
        except KeyError:
            raise client_error("ValidationException", "Key", "missing key attribute")

    def _keep(self, projection, names, used):
        if not projection:
            return None
        p = _Parser(projection, names, {}, used)
        keep = []
        while p.peek()[0]:
            keep.append(p.path()[1][0])
            if p.peek() == ("P", ","):
                p.take()
        return keep

    def _out(self, item, keep=None):
        item = copy.deepcopy(item)
        if keep is not None:
            item = {k: v for k, v in item.items() if k in keep}
        return _to_resource(item)

    def _condition(self, expr, names, values, item, op, used):
        if not expr:
            return
        node = _Parser(expr, names, values, used).cond()
        if not _test(node, item or {}):
            raise client_error("ConditionalCheckFailedException", op, "The conditional request failed")

    # ---- API
    def get_item(self, Key, ConsistentRead=False, ProjectionExpression=None, ExpressionAttributeNames=None):
        self.calls.append(("get_item", Key))
        it = self.items.get(self._key(Key))
        keep = self._keep(ProjectionExpression, ExpressionAttributeNames, set())
        return {"Item": self._out(it, keep)} if it else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None,
                 ExpressionAttributeValues=None):
        _check_types(Item)
        self.calls.append(("put_item", self._key(Item)))
        used = set()
        self._condition(ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues,
                        self.items.get(self._key(Item)), "PutItem", used)
        _check_unused(ExpressionAttributeNames, ExpressionAttributeValues, used, "PutItem")
        self.items[self._key(Item)] = copy.deepcopy(Item)
        return {}

    def delete_item(self, Key, **kw):
        self.items.pop(self._key(Key), None)
        return {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None):
        _check_types(ExpressionAttributeValues or {})
        self.calls.append(("update_item", self._key(Key)))
        k = self._key(Key)
        existing = self.items.get(k)
        used = set()
        self._condition(ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, existing,
                        "UpdateItem", used)
        item = copy.deepcopy(existing) if existing else dict(Key)
        actions = _Parser(UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, used).update()
        _check_unused(ExpressionAttributeNames, ExpressionAttributeValues, used, "UpdateItem")
        for a in actions:
            segs = a[1][1]
            if segs[0] in (self.hk, self.rk):
                raise client_error("ValidationException", "UpdateItem", "cannot update key attribute")
            parent = _get(item, segs[:-1]) if len(segs) > 1 else item
            if parent is _MISSING or not isinstance(parent, dict):
                raise client_error("ValidationException", "UpdateItem",
                                   "The document path provided in the update expression is invalid for update")
            if a[0] == "SET":
                parent[segs[-1]] = copy.deepcopy(_eval(a[2], existing or {}))
            elif a[0] == "REMOVE":
                parent.pop(segs[-1], None)
            elif a[0] == "ADD":
                inc = _eval(a[2], existing or {})
                parent[segs[-1]] = parent.get(segs[-1], 0) + inc
        self.items[k] = item
        return {"Attributes": _to_resource(copy.deepcopy(item))} if ReturnValues else {}

    def _ordered(self, index=None):
        hk, rk = (self.hk, self.rk) if not index else self.indexes[index]
        rows = [it for it in self.items.values() if hk in it and (rk is None or rk in it)]
        return sorted(rows, key=lambda it: (str(it[hk]), str(it.get(rk, "")) if rk else "")), hk, rk

    def query(self, KeyConditionExpression, ExpressionAttributeValues=None, ExpressionAttributeNames=None,
              IndexName=None, ScanIndexForward=True, Limit=None, ExclusiveStartKey=None, ProjectionExpression=None,
              ConsistentRead=False):
        if not isinstance(KeyConditionExpression, str):
            raise TypeError("use string KeyConditionExpression in this project")
        self.calls.append(("query", IndexName))
        used = set()
        node = _Parser(KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, used).cond()
        rows, hk, rk = self._ordered(IndexName)
        rows = [r for r in rows if _test(node, r)]
        if not ScanIndexForward:
            rows.reverse()
        return self._page(rows, Limit, ExclusiveStartKey, ProjectionExpression, ExpressionAttributeNames, used,
                          "Query", ExpressionAttributeValues)

    def scan(self, Limit=None, ExclusiveStartKey=None, ProjectionExpression=None, ExpressionAttributeNames=None,
             Segment=None, TotalSegments=None):
        self.calls.append(("scan", Segment))
        rows, _, _ = self._ordered()
        if TotalSegments:
            rows = [r for i, r in enumerate(rows) if i % TotalSegments == Segment]
        return self._page(rows, Limit, ExclusiveStartKey, ProjectionExpression, ExpressionAttributeNames, set(),
                          "Scan", None)

    def _page(self, rows, limit, start, projection, names, used, op, values):
        if start is not None:
            keys = [self._key(r) for r in rows]
            rows = rows[keys.index(self._key(start)) + 1:]
        more = limit is not None and len(rows) > limit
        rows = rows[:limit] if limit is not None else rows
        keep = self._keep(projection, names, used)
        out = {"Items": [self._out(r, keep) for r in rows], "Count": len(rows)}
        _check_unused(names, values, used, op)
        if more:
            out["LastEvaluatedKey"] = {k: rows[-1][k] for k in (self.hk, self.rk) if k}
        return out

    def batch_writer(self):
        table = self

        class _BW:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def put_item(self, Item):
                table.put_item(Item=Item)

            def delete_item(self, Key):
                table.delete_item(Key=Key)

        return _BW()


# ---------------------------------------------------------------- S3
class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def put_object(self, Bucket, Key, Body, **kw):
        data = Body.encode() if isinstance(Body, str) else bytes(Body)
        self.objects[(Bucket, Key)] = {"Body": data, **kw}
        self.puts.append(Key)
        return {"ETag": hashlib.md5(data).hexdigest()}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise client_error("NoSuchKey", "GetObject")
        o = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(o["Body"]), "ContentType": o.get("ContentType")}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://{Params['Bucket']}.s3.fake/{Params['Key']}?op={op}&X-Amz-Expires={ExpiresIn}"


# ---------------------------------------------------------------- Bedrock
def hashed_embedding(text, dims=256):
    from docintel_common.retrieval import tokenize
    v = [0.0] * dims
    for t in tokenize(text):
        v[int(hashlib.md5(t.encode()).hexdigest(), 16) % dims] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeBedrock:
    """``responder(system, messages) -> str``; messages are Converse-format dicts."""

    def __init__(self, responder=None, embeddings=True):
        self.responder = responder or (lambda system, messages: "{}")
        self.embeddings = embeddings
        self.calls = []
        self.embed_calls = 0

    def converse(self, modelId, system, messages, inferenceConfig):
        sys_text = system[0]["text"]
        self.calls.append({"system": sys_text, "messages": messages})
        out = self.responder(sys_text, messages)
        return {"output": {"message": {"role": "assistant", "content": [{"text": out}]}},
                "usage": {"inputTokens": 100, "outputTokens": 20}}

    def invoke_model(self, modelId, body, contentType=None, accept=None):
        if not self.embeddings:
            raise client_error("AccessDeniedException", "InvokeModel")
        self.embed_calls += 1
        text = json.loads(body)["inputText"]
        return {"body": io.BytesIO(json.dumps({"embedding": hashed_embedding(text)}).encode())}


# ---------------------------------------------------------------- Comprehend
class FakeComprehend:
    NAMES = ("John Smith", "Jane Doe", "Priya Sharma")
    PATTERNS = [
        ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
        ("PHONE", re.compile(r"\+?\d{3}[-. ]\d{3}[-. ]\d{4}")),
        ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
        ("BANK_ACCOUNT_NUMBER", re.compile(r"(?<=Account Number: )\d{6,}")),
        ("ADDRESS", re.compile(r"\d+ [A-Z][a-z]+ (?:Street|Avenue|Road)")),
    ]

    def __init__(self):
        self.pii_calls = []

    def detect_dominant_language(self, Text):
        return {"Languages": [{"LanguageCode": "en", "Score": 0.99}]}

    def detect_entities(self, Text, LanguageCode):
        ents = []
        for m in re.finditer(r"\b(?:[A-Z][a-z]+ ){1,2}[A-Z][a-z]+\b", Text):
            ents.append({"Type": "PERSON" if m.group(0) in self.NAMES else "ORGANIZATION", "Text": m.group(0),
                         "Score": 0.91, "BeginOffset": m.start(), "EndOffset": m.end()})
        return {"Entities": ents}

    def detect_key_phrases(self, Text, LanguageCode):
        words = re.findall(r"[A-Za-z]{5,}", Text)
        return {"KeyPhrases": [{"Text": w, "Score": 0.8} for w in words[:10]]}

    def detect_pii_entities(self, Text, LanguageCode):
        self.pii_calls.append(len(Text))
        ents = []
        for name in self.NAMES:
            for m in re.finditer(re.escape(name), Text):
                ents.append({"Type": "NAME", "Score": 0.99, "BeginOffset": m.start(), "EndOffset": m.end()})
        for typ, rx in self.PATTERNS:
            for m in rx.finditer(Text):
                ents.append({"Type": typ, "Score": 0.97, "BeginOffset": m.start(), "EndOffset": m.end()})
        return {"Entities": sorted(ents, key=lambda e: e["BeginOffset"])}

    def classify_document(self, Text, EndpointArn):
        return {"Classes": []}


# ---------------------------------------------------------------- Textract
def make_blocks(pages, kvs=(), tables=()):
    """Build Textract-like blocks.

    pages:  [[("line text", confidence), ...], ...]
    kvs:    [(key, value, key_conf, value_conf, page), ...]
    tables: [[["a", "b"], ["c", "d"]], ...]  (page 1)
    """
    blocks, n = [], 0

    def nid():
        nonlocal n
        n += 1
        return f"b{n}"

    def words(text, conf, page):
        ids = []
        for w in text.split():
            i = nid()
            blocks.append({"Id": i, "BlockType": "WORD", "Text": w, "Confidence": conf, "Page": page})
            ids.append(i)
        return ids

    for p, lines in enumerate(pages, start=1):
        blocks.append({"Id": nid(), "BlockType": "PAGE", "Page": p})
        for text, conf in lines:
            blocks.append({"Id": nid(), "BlockType": "LINE", "Text": text, "Confidence": conf, "Page": p})
    for key, value, kc, vc, page in kvs:
        kid, vid = nid(), nid()
        blocks.append({"Id": kid, "BlockType": "KEY_VALUE_SET", "EntityTypes": ["KEY"], "Confidence": kc,
                       "Page": page, "Relationships": [{"Type": "CHILD", "Ids": words(key, kc, page)},
                                                       {"Type": "VALUE", "Ids": [vid]}]})
        blocks.append({"Id": vid, "BlockType": "KEY_VALUE_SET", "EntityTypes": ["VALUE"], "Confidence": vc,
                       "Page": page, "Relationships": [{"Type": "CHILD", "Ids": words(value, vc, page)}]})
    for table in tables:
        tid = nid()
        cells = []
        for r, row in enumerate(table, start=1):
            for c, text in enumerate(row, start=1):
                cid = nid()
                blocks.append({"Id": cid, "BlockType": "CELL", "RowIndex": r, "ColumnIndex": c, "Page": 1,
                               "Relationships": [{"Type": "CHILD", "Ids": words(text, 99.0, 1)}]})
                cells.append(cid)
        blocks.append({"Id": tid, "BlockType": "TABLE", "Page": 1, "Relationships": [{"Type": "CHILD", "Ids": cells}]})
    return blocks


class FakeTextract:
    def __init__(self):
        self.docs = {}       # s3 key -> blocks

    def analyze_document(self, Document, FeatureTypes):
        return {"Blocks": self.docs[Document["S3Object"]["Name"]]}

    def start_document_analysis(self, DocumentLocation, FeatureTypes):
        return {"JobId": DocumentLocation["S3Object"]["Name"]}

    def get_document_analysis(self, JobId, NextToken=None):
        return {"JobStatus": "SUCCEEDED", "Blocks": self.docs[JobId]}
