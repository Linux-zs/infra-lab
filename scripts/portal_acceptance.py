"""Independent real browser-entry acceptance. Uses a loopback SSH tunnel."""
import http.client
import json
import time
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8088/api"
evidence = {"checks": []}


def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def record(name, value):
    evidence["checks"].append({"name": name, "value": value})
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/portal-acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(name + ": " + json.dumps(value, ensure_ascii=False), flush=True)


lab = api("/labs", "POST", {"name": "真实网站入口验收"})
lab_id = lab["id"]
evidence["lab_id"] = lab_id
url = f"/labs/{lab_id}"
nodes = [{"id": "gateway", "kind": "nginx", "position": {"x": 60, "y": 120}, "memory_mb": 64, "cpu": .25}, {"id": "web-app", "kind": "app", "position": {"x": 420, "y": 120}, "memory_mb": 64, "cpu": .25}]
lab = api(url + "/draft", "PUT", {"base_revision": 0, "topology": {"nodes": nodes, "edges": [{"source": "gateway", "target": "web-app", "weight": 1}]}})


def task(kind, **extra):
    created = api(url + "/tasks", "POST", {"kind": kind, "revision": lab["revision"], "request_id": uuid.uuid4().hex, **extra})
    for _ in range(240):
        current = next(t for t in api(url + "/tasks") if t["id"] == created["id"])
        if current["status"] not in ("queued", "running"):
            assert current["status"] == "succeeded", current
            return current
        time.sleep(.5)
    raise TimeoutError(created["id"])


task("apply")
entry = api(url + "/entries", "POST", {"node_id": "gateway", "trace": True})
evidence["entry"] = entry
record("entry_created", entry["url"])


def visit(path="/", method="GET", body=None, hostname=None):
    con = http.client.HTTPConnection("127.0.0.1", 8089, timeout=35)
    con.request(method, path, body=body, headers={"Host": (hostname or entry["hostname"]) + ":8089", "Content-Type": "application/x-www-form-urlencoded"})
    response = con.getresponse()
    value = {"status": response.status, "headers": dict(response.getheaders()), "body": response.read().decode(errors="replace")}
    con.close()
    return value


def detail(request_id):
    for _ in range(120):
        value = api(url + "/requests/" + request_id)
        if value["status"] != "running":
            return value
        time.sleep(.2)
    raise TimeoutError(request_id)


home = visit()
assert home["status"] == 200, home
assert "web-app" in home["body"] and "x-request-id" in home["headers"]
for path in ("/details", "/assets/site.css", "/assets/site.js", "/search?q=hello", "/api/instance"):
    response = visit(path)
    assert response["status"] == 200, response
    trace = detail(response["headers"]["x-request-id"])
    assert trace["result"]["trace_complete"], trace
    assert trace["source"] == "browser" and not trace["result"]["body"]
record("pages_assets_get_and_real_traces", True)
submitted = visit("/submit", "POST", "message=private-form-value")
assert submitted["status"] == 200 and "private-form-value" in submitted["body"]
trace = detail(submitted["headers"]["x-request-id"])
assert trace["method"] == "POST" and "private-form-value" not in json.dumps(trace)
assert len([e for e in trace["events"] if e["kind"] == "app.received"]) == 1
record("post_not_replayed_or_persisted", trace["id"])
redirect = visit("/redirect")
assert redirect["status"] == 302 and redirect["headers"]["location"].endswith("/details")
record("redirect", redirect["headers"]["location"])
assert visit(hostname="evil.example")["status"] == 404
api(url + "/entries/" + entry["id"], "PATCH", {"trace": False})
off = visit("/api/instance")
assert detail(off["headers"]["x-request-id"])["events"] == []
record("trace_off", True)
task("stop", node_id="gateway")
assert visit()["status"] == 503
task("start", node_id="gateway")
assert visit()["status"] == 200
record("stopped_entry_no_automatic_start", True)
api(url + "/entries/" + entry["id"], "DELETE")
assert visit()["status"] == 410
entry = api(url + "/entries", "POST", {"node_id": "gateway", "trace": True})
evidence["entry"] = entry
record("revoke_and_new_origin", entry["url"])
# Keep this test lab temporarily for browser validation. Explicit cleanup follows.
