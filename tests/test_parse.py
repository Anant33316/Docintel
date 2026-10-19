import os, sys
for _k, _v in dict(TABLE_NAME="t", BUCKET_NAME="b", BEDROCK_MODEL_ID="m", AWS_DEFAULT_REGION="us-east-1").items():
    os.environ.setdefault(_k, _v)   # conftest.py may already have configured the test environment
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "processor"))
import app  # noqa: E402


def W(i, t): return {"Id": i, "BlockType": "WORD", "Text": t}


BLOCKS = [
    {"Id": "p", "BlockType": "PAGE", "Page": 1},
    {"Id": "l1", "BlockType": "LINE", "Text": "Invoice Total: 40"},
    W("w1", "Total"), W("w2", "40"), W("w3", "A"), W("w4", "B"),
    {"Id": "k", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["KEY"],
     "Relationships": [{"Type": "CHILD", "Ids": ["w1"]}, {"Type": "VALUE", "Ids": ["v"]}]},
    {"Id": "v", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["VALUE"], "Relationships": [{"Type": "CHILD", "Ids": ["w2"]}]},
    {"Id": "t", "BlockType": "TABLE", "Relationships": [{"Type": "CHILD", "Ids": ["c1", "c2"]}]},
    {"Id": "c1", "BlockType": "CELL", "RowIndex": 1, "ColumnIndex": 1, "Relationships": [{"Type": "CHILD", "Ids": ["w3"]}]},
    {"Id": "c2", "BlockType": "CELL", "RowIndex": 1, "ColumnIndex": 2, "Relationships": [{"Type": "CHILD", "Ids": ["w4"]}]},
]


def test_parse_blocks():
    r = app.parse_blocks(BLOCKS)
    assert r["text"] == "Invoice Total: 40"
    assert r["keyValues"] == {"Total": "40"}
    assert r["tables"] == [[["A", "B"]]]
    assert r["pages"] == 1


def test_llm_json_fallback(monkeypatch):
    monkeypatch.setattr(app, "converse", lambda *a, **k: 'noise {"summary":"s","intent":"i","category":"bogus"} tail')
    r = app.summarize_and_classify("x")
    assert r["category"] == "other" and r["summary"] == "s"
