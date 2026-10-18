"""Real hot response configuration acceptance; leaves dedicated lab for UI checks."""
import http.client
import json
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8088/api"
evidence = {}


def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def record(name, value):
    evidence[name] = value
    Path("artifacts/response-acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(name, json.dumps(value, ensure_ascii=False), flush=True)


resume = os.getenv("INFRA_RESPONSE_ACCEPTANCE_LAB")
lab = api("/labs/" + resume) if resume else api("/labs", "POST", {"name": "应用响应验收"})
assert lab["name"] == "应用响应验收"
root = "/labs/" + lab["id"]
record("lab_id", lab["id"])
nodes = [{"id": name, "kind": "nginx" if i == 0 else "app", "position": {"x": 70 if i == 0 else 430, "y": 150 if i == 0 else 50 + 200*(i-1)}, "memory_mb": 64, "cpu": .25} for i, name in enumerate(["gateway", "app-a", "app-b"])]
lab = api(root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": {"nodes": nodes, "edges": [{"source": "gateway", "target": "app-a", "weight": 1}]}})
topology = lab["draft"]
topology["nodes"][0].update(mode="manual", config=topology["nodes"][0]["config"].replace("proxy_read_timeout 2s", "proxy_read_timeout 10s"))
api(root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": topology})


def task(kind, expect="succeeded", **extra):
    body = {"request_id": uuid.uuid4().hex, "kind": kind, "revision": api(root)["revision"], **extra}
    first = api(root + "/tasks", "POST", body)
    assert api(root + "/tasks", "POST", body)["id"] == first["id"]
    for _ in range(240):
        current = next(t for t in api(root + "/tasks") if t["id"] == first["id"])
        if current["status"] not in ("queued", "running"):
            assert current["status"] == expect, current
            return current
        time.sleep(.2)
    raise TimeoutError(first)


task("apply")
entry = api(root + "/entries", "POST", {"node_id": "gateway", "trace": True})
record("entry", entry)


def visit(method="GET", path="/page"):
    conn = http.client.HTTPConnection("127.0.0.1", 8089, timeout=15)
    conn.request(method, path, body="message=demo" if method == "POST" else None, headers={"Host": entry["hostname"] + ":8089"})
    response = conn.getresponse()
    value = {"status": response.status, "headers": response.getheaders(), "body": response.read().decode()}
    conn.close()
    return value


def configure(node="app-a", apply=True, **patch):
    path = root + "/nodes/" + node + "/response"
    saved = api(path)
    config = {"mode": "custom", "status": 200, "format": "json", "body": '{"hello":"世界"}', "headers": [], "delay_ms": 0, **patch}
    saved = api(path, "PUT", {"base_version": saved["version"], "config": config})
    if apply:
        task("apply_response", node_id=node, response_version=saved["version"])
    return saved["version"]


for _ in range(100):
    observation = api(root).get("observation")
    if observation and len(observation["nodes"]) == 3:
        break
    time.sleep(.2)
initial = {n["id"]: n["container_id"] for n in api(root)["observation"]["nodes"]}
version = configure(headers=[{"name": "X-Demo", "value": "yes"}, {"name": "Set-Cookie", "value": "a=1; Path=/"}, {"name": "Set-Cookie", "value": "b=2; Path=/"}])
for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
    result = visit(method)
    assert result["status"] == 200, result
    headers = {k.lower(): v for k, v in result["headers"]}
    assert headers["x-infra-response-version"] == str(version) and headers["x-infra-node"] == "app-a"
    assert result["body"] == ("" if method == "HEAD" else '{"hello":"世界"}')
    assert len([h for h in result["headers"] if h[0].lower() == "set-cookie"]) == 2
record("methods_json_headers_cookies", True)
for format, body, status in [("text", "文本正文", 404), ("html", "<h1>自定义页面</h1>", 503), ("text", "", 204), ("text", "", 205), ("text", "", 304)]:
    configure(format=format, body=body, status=status)
    result = visit()
    assert result["status"] == status and result["body"] == body, result
record("formats_and_status_codes", True)
configure(format="text", body="", status=302, headers=[{"name": "Location", "value": "/next"}])
assert dict(visit()["headers"])["location"] == "/next"
slow_version = configure(format="text", body="旧响应", delay_ms=8000)
before = {r["id"] for r in api(root + "/requests")}
slow = []
thread = threading.Thread(target=lambda: slow.append(visit(path="/slow")))
thread.start()
for _ in range(50):
    requests = [r for r in api(root + "/requests") if r["id"] not in before]
    if requests:
        detail = api(root + "/requests/" + requests[0]["id"])
        if any(e["kind"] == "app.processing" for e in detail["events"]):
            break
    time.sleep(.1)
else:
    raise AssertionError("No live app.processing")
assert thread.is_alive()
configure(format="text", body="新响应")
thread.join()
assert slow[0]["body"] == "旧响应" and visit()["body"] == "新响应"
record("inflight_snapshot", slow_version)
# Saving a topology edit does not apply it when applying response config.
lab = api(root)
topology = lab["draft"]
topology["nodes"][1]["delay_ms"] = 777
api(root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": topology})
configure(format="text", body="独立生效")
lab = api(root)
assert lab["applied"]["nodes"][1]["delay_ms"] == 0 and lab["draft"]["nodes"][1]["delay_ms"] == 777
assert {n["id"]: n["container_id"] for n in lab["observation"]["nodes"]} == initial
task("reload", node_id="gateway")
assert visit()["body"] == "独立生效"
record("no_recreate_no_topology_apply_and_reload", initial)
task("stop", node_id="app-a")
pending = configure(apply=False, format="text", body="尚未应用")
task("apply_response", expect="failed", node_id="app-a", response_version=pending)
task("start", node_id="app-a")
assert visit()["body"] == "独立生效"
task("apply")  # Rebuild app-a due to saved 777 ms delay; restore applied response only.
assert visit()["body"] == "独立生效"
record("stopped_and_rebuild_restore_applied_only", True)
configure(node="app-b", format="text", body="备用应用")
configure(status=503, format="text", body="故障应用")
lab = api(root)
topology = lab["draft"]
topology["nodes"][0]["config"] = topology["nodes"][0]["config"].replace("upstream backends {", "upstream backends {\n    server app-b:8080 backup;")
api(root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": topology})
task("apply")
result = visit()
assert result["status"] == 200 and result["body"] == "备用应用", result
record("real_nginx_retry", result)
configure(mode="website")
time.sleep(6)
assert "实验网站" in visit()["body"]
record("default_site_restored", True)
record("ready_for_browser", lab["id"])
