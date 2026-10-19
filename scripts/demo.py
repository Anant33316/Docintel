#!/usr/bin/env python3
"""End-to-end demo: python scripts/demo.py <api-url> <api-key> <file> ["question"] [--tour]

Without --tour this behaves exactly like the original demo (upload, wait, show, ask).
With --tour it also walks through the v2 features: chat follow-up, confidence/review,
key dates, search, PII detection, export and the dashboard stats.
"""
import json, os, sys, time, urllib.request


def call(method, url, key=None, body=None, raw=None, ctype=None):
    headers = {}
    if key:
        headers["x-api-key"] = key
        headers["X-User-Id"] = "demo"
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt else {}


args = [a for a in sys.argv[1:] if a != "--tour"]
tour = "--tour" in sys.argv
api, key, path = args[0].rstrip("/"), args[1], args[2]
question = args[3] if len(args) > 3 else "What is this document about?"

up = call("POST", f"{api}/documents/upload-url", key, {"fileName": os.path.basename(path)})
call("PUT", up["uploadUrl"], raw=open(path, "rb").read(), ctype=up["contentType"])
print("uploaded", up["key"])

doc = None
for _ in range(60):
    time.sleep(5)
    mine = [d for d in call("GET", f"{api}/documents", key)["documents"] if d["fileName"] == os.path.basename(up["key"])]
    if mine and mine[0]["status"] in ("COMPLETED", "FAILED"):
        doc = mine[0]
        break
    print("waiting...", mine[0]["status"] if mine else "queued")
if not doc:
    sys.exit("timed out")

full = call("GET", f"{api}/documents/{doc['docId']}", key)
print(json.dumps(full, indent=2))
if full["status"] != "COMPLETED":
    sys.exit(0)

d = doc["docId"]
print("\nQ:", question)
a = call("POST", f"{api}/documents/{d}/ask", key, {"question": question})
print("A:", a["answer"])
for c in a.get("citations", []):
    print(f"   source: {c['fileName']} p.{c['page']} (relevance {c['relevance']}): {c['snippet']}")

if tour:
    print("\n== Chat follow-up ==")
    c1 = call("POST", f"{api}/documents/{d}/chat", key, {"question": question})
    c2 = call("POST", f"{api}/documents/{d}/chat", key, {"question": "Are there any deadlines?", "sessionId": c1["sessionId"]})
    print("session", c1["sessionId"], "->", c2["answer"], f"[confidence {c2['confidence']}]")

    print("\n== Confidence / review ==")
    f = call("GET", f"{api}/documents/{d}/fields", key)
    print(json.dumps(f["summary"], indent=2))
    for fld in f["fields"][:10]:
        flag = "REVIEW" if fld["requiresVerification"] else "ok"
        print(f"  {fld['key']}: {fld['aiValue']}  ({fld['confidence']}%, {flag})")

    print("\n== Key dates & actions ==")
    for kd in full.get("keyDates", []):
        print(f"  {kd['type']}: {kd.get('isoDate') or kd['date']} - {kd.get('description')}  grounded={kd['grounded']}")
    for act in full.get("actions", []):
        print(f"  ACTION {act['action']} by {act.get('deadline')} ({act.get('responsibleParty')})")

    print("\n== Search ==")
    term = (full.get("keyPhrases") or ["invoice"])[0]
    s = call("GET", f"{api}/search?q={urllib.request.quote(term)}", key)
    for r in s["results"][:5]:
        print(f"  {r['score']:.2f} {r['fileName']}: {r['snippets'][0]['text'] if r['snippets'] else ''}")

    print("\n== PII detection ==")
    p = call("POST", f"{api}/documents/{d}/pii", key, {})
    print("  found:", p["counts"])

    print("\n== Export ==")
    for fmt in ("json", "csv", "pdf"):
        e = call("GET", f"{api}/documents/{d}/export?format={fmt}", key)
        print(f"  {fmt}: {e['bytes']} bytes -> {e['downloadUrl'][:80]}...")

    print("\n== Dashboard ==")
    st = call("GET", f"{api}/stats", key)
    print(json.dumps({k: st[k] for k in ("documents", "categories", "processingTimeSeconds")}, indent=2))
