"""Fault/recovery checks for the dedicated portal_acceptance lab only."""
import http.client
import json
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

seed = json.loads(Path("artifacts/portal-acceptance.json").read_text(encoding="utf-8"))
lab_id, entry = seed["lab_id"], seed["entry"]
root = f"/labs/{lab_id}"
checks = {}


def api(path, method="GET", body=None):
    req = urllib.request.Request("http://127.0.0.1:8088/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def record(name, value):
    checks[name] = value
    Path("artifacts/portal-faults.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(name, json.dumps(value, ensure_ascii=False), flush=True)


def submit(kind, **extra):
    lab = api(root)
    return api(root + "/tasks", "POST", {"kind": kind, "revision": lab["revision"], "request_id": uuid.uuid4().hex, **extra})["id"]


def wait_task(task_id):
    for _ in range(240):
        task = next(t for t in api(root + "/tasks") if t["id"] == task_id)
        if task["status"] not in ("queued", "running"):
            assert task["status"] == "succeeded", task
            return task
        time.sleep(.25)
    raise TimeoutError(task_id)


def configure(delay=0, errors=0, config=None):
    lab = api(root)
    assert lab["name"] == "真实网站入口验收"
    topology = lab["draft"]
    for node in topology["nodes"]:
        if node["kind"] == "app":
            node.update(delay_ms=delay, error_rate=errors)
        else:
            node.update(mode="manual" if config else "generated", config=config or "")
    api(root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": topology})
    wait_task(submit("apply"))


def visit(path):
    con = http.client.HTTPConnection("127.0.0.1", 8089, timeout=40)
    result = {}
    try:
        con.request("GET", path, headers={"Host": entry["hostname"] + ":8089"})
        response = con.getresponse()
        result.update(status=response.status, id=response.getheader("X-Request-ID"))
        result["body"] = response.read()
    except (OSError, http.client.HTTPException) as exc:
        result["error"] = type(exc).__name__
    finally:
        con.close()
    return result


def wait_detail(request_id):
    for _ in range(240):
        value = api(root + "/requests/" + request_id)
        if value["status"] != "running":
            return value
        time.sleep(.2)
    raise TimeoutError(request_id)


def launch(path):
    holder = {}
    thread = threading.Thread(target=lambda: holder.update(visit(path)))
    thread.start()
    for _ in range(150):
        records = api(root + "/requests")
        record = next((r for r in records if r["path"] == path), None)
        if record:
            detail = api(root + "/requests/" + record["id"])
            if any(e["kind"] == "app.processing" for e in detail["events"]):
                return thread, holder, record["id"]
        time.sleep(.1)
    raise AssertionError("No live app.processing")


configure(delay=1200)
load_id = submit("load", node_id="gateway", count=30, concurrency=1, duration=15)
for _ in range(40):
    if next(t for t in api(root + "/tasks") if t["id"] == load_id)["status"] == "running":
        break
    time.sleep(.2)
thread, holder, request_id = launch("/details?phase=queue-" + uuid.uuid4().hex[:6])
assert thread.is_alive(), "Processing event was not live"
thread.join(15)
detail = wait_detail(request_id)
assert holder["status"] == 200 and detail["result"]["trace_complete"]
assert next(t for t in api(root + "/tasks") if t["id"] == load_id)["status"] == "running"
record("live_trace_independent_of_task_queue", request_id)
wait_task(load_id)

configure(delay=5000)
# Raise Nginx read timeout so a single app remains processing during restarts.
good = api(root)["draft"]["nodes"][0]["config"].replace("proxy_read_timeout 2s", "proxy_read_timeout 15s")
configure(delay=5000, config=good)
thread, holder, request_id = launch("/details?phase=worker-" + uuid.uuid4().hex[:6])
subprocess.run(["ssh", "-o", "BatchMode=yes", "root@188.66.8.20", "docker restart -t 0 infra-lab-worker-1"], check=True, capture_output=True)
thread.join(20)
detail = wait_detail(request_id)
assert holder["status"] == 200 and detail["result"]["trace_complete"]
record("worker_restart_does_not_interrupt_browser_trace", request_id)
thread, holder, request_id = launch("/details?phase=collector-" + uuid.uuid4().hex[:6])
subprocess.run(["ssh", "-o", "BatchMode=yes", "root@188.66.8.20", "docker restart -t 0 infra-lab-portal-1"], check=True, capture_output=True)
thread.join(20)
detail = wait_detail(request_id)
assert detail["status"] == "interrupted"
assert any(e["kind"] == "worker.interrupted" for e in detail["events"])
record("collector_interruption_explicit_no_replay", {"id": request_id, "status": detail["status"]})

configure(errors=100)
error = visit("/")
assert error["status"] == 503 and b"503" in error["body"]
record("real_503_body", error["id"])

configure()
good = api(root)["draft"]["nodes"][0]["config"]
configure(config=good.replace("access_log /dev/stdout lab;", "access_log off;"))
missing = visit("/details")
detail = wait_detail(missing["id"])
assert not detail["result"]["trace_complete"]
assert not any(e["kind"] == "nginx.completed" for e in detail["events"])
record("disabled_log_incomplete", missing["id"])

config = good.replace("listen 8080;", "listen 8080; location = /large { root /tmp; } location = /slow-file { alias /tmp/large; limit_rate 1024; }")
configure(config=config)
code = f"from infra.runtime import Runtime,archive; r=Runtime(); c=r.node('{lab_id}','gateway'); c.put_archive('/tmp',archive('large','x'*1100000))"
subprocess.run(["ssh", "-o", "BatchMode=yes", "root@188.66.8.20", "docker exec -i infra-lab-api-1 python -"], input=code, text=True, check=True)
large = visit("/large")
assert len(large["body"]) == 1100000
record("website_response_not_truncated_at_1mb", len(large["body"]))
slow = visit("/slow-file")
detail = wait_detail(slow["id"])
assert detail["result"]["error_kind"] == "timeout", detail
assert detail["result"]["latency_ms"] >= 29000
record("30_second_stream_deadline", {"id": slow["id"], "error_kind": detail["result"]["error_kind"], "latency_ms": detail["result"]["latency_ms"]})
configure()
record("restored_for_browser", entry["url"])
