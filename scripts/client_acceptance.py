"""Real-container client/trace/lifecycle acceptance in a separate labelled lab.

Requires the existing SSH tunnel and SSH key. Never mutates another lab.
Leaves its healthy lab for browser inspection; cleanup is explicit afterward.
"""
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8088/api"
OUTPUT = Path("artifacts/client-acceptance.json")
evidence = {"checks": []}


def api(path, method="GET", body=None):
    request = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def remote(code):
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "root@188.66.8.20",
                             "docker exec -i infra-lab-api-1 python -"],
                            input=code, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def record(name, value):
    evidence["checks"].append({"name": name, "time": time.time(), "value": value})
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(name + ": " + json.dumps(value, ensure_ascii=False), flush=True)


baseline = remote("import docker,json; print(json.dumps({c.id:c.status for c in docker.from_env().containers.list(all=True)}))")
lab = api("/labs", "POST", {"name": "客户端与链路验收"})
lab_id = lab["id"]
evidence["lab_id"] = lab_id
url = f"/labs/{lab_id}"


def save(topology):
    global lab
    lab = api(url + "/draft", "PUT", {"base_revision": lab["revision"], "topology": topology})


def submit(kind, **extra):
    action = {"kind": kind, "revision": lab["revision"], "request_id": uuid.uuid4().hex, **extra}
    first = api(url + "/tasks", "POST", action)
    assert api(url + "/tasks", "POST", action)["id"] == first["id"]
    return first["id"]


def wait(task_id, expected="succeeded"):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        task = next(t for t in api(url + "/tasks") if t["id"] == task_id)
        if task["status"] not in ("queued", "running"):
            assert task["status"] == expected, task
            return task
        time.sleep(.25)
    raise TimeoutError(task_id)


def task(kind, **extra):
    return wait(submit(kind, **extra))


def request(path="/page", trace=True):
    result = task("client_request", node_id="gateway", path=path, trace=trace)
    return api(url + "/requests/" + result["id"])


topology = {"nodes": [
    {"id": "gateway", "kind": "nginx", "position": {"x": 60, "y": 180}},
    {"id": "app-a", "kind": "app", "position": {"x": 400, "y": 80}, "delay_ms": 1500},
    {"id": "app-b", "kind": "app", "position": {"x": 400, "y": 300}, "delay_ms": 1500},
], "edges": [{"source": "gateway", "target": n, "weight": 1} for n in ("app-a", "app-b")]}
for node in topology["nodes"]:
    node.update(memory_mb=64, cpu=.25)
save(topology)
task("apply")
record("created", lab_id)
task_id = submit("client_request", node_id="gateway", path="/page?from=acceptance", trace=True)
live = False
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    detail = api(url + "/requests/" + task_id)
    if detail["status"] == "running" and any(e["kind"] == "app.processing" for e in detail["events"]):
        live = True
    if detail["status"] not in ("queued", "running"):
        break
    time.sleep(.15)
wait(task_id)
assert live, "No app.processing event observed before completion"
result = detail["result"]
assert result["status_code"] == 200 and task_id in result["body"] and result["trace_complete"]
nginx = next(e for e in detail["events"] if e["kind"] == "nginx.completed")
assert nginx["data"]["attempts"][-1]["node_id"] in result["body"]
record("live_processing_and_same_response_identity", {"request_id": task_id, "events": detail["events"]})
plain = request("/api?x=1", False)
assert not plain["events"]
assert json.loads(plain["result"]["body"])["request_id"] == plain["id"]
record("json_and_trace_off", plain["id"])

# Stop/start must preserve IDs and keep unrelated resources intact.
before = remote(f"import docker,json; print(json.dumps({{c.id:c.status for c in docker.from_env().containers.list(all=True,filters={{'label':['infra.owner=infra-lab-v1','infra.lab={lab_id}','infra.role=node']}})}}))")
task("stop_lab")
stopped = remote(f"import docker,json; print(json.dumps({{c.id:c.status for c in docker.from_env().containers.list(all=True,filters={{'label':['infra.owner=infra-lab-v1','infra.lab={lab_id}','infra.role=node']}})}}))")
assert set(before) == set(stopped) and set(stopped.values()) == {"exited"}
assert all(n["status"] == "skipped" for n in task("stop_lab")["result"]["nodes"])
failed = request(trace=True)
assert failed["result"]["error_kind"] in ("connection", "timeout")
task("start_lab")
assert all(n["status"] == "skipped" for n in task("start_lab")["result"]["nodes"])
assert request()["result"]["status_code"] == 200
after = remote(f"import docker,json; print(json.dumps({{c.id:c.status for c in docker.from_env().containers.list(all=True)}}))")
assert all(after.get(key) == value for key, value in baseline.items())
assert all(after.get(key) == "running" for key in before)
record("lifecycle_preserves_ids_and_other_labs", {"before": before, "stopped": stopped})

# Deterministic primary/backup: stop app-a and inspect actual Nginx retry evidence.
topology = lab["draft"]
for n in topology["nodes"]:
    if n["kind"] == "app":
        n["delay_ms"] = 0
gateway = topology["nodes"][0]
gateway["mode"] = "manual"
gateway["config"] = gateway["config"].replace("server app-b:8080 weight=1", "server app-b:8080 backup")
save(topology)
task("apply")
task("stop", node_id="app-a")
retry = request()
attempts = next(e for e in retry["events"] if e["kind"] == "nginx.completed")["data"]["attempts"]
assert len(attempts) == 2 and attempts[-1]["node_id"] == "app-b", attempts
assert retry["result"]["status_code"] == 200
record("stopped_upstream_retry", {"request_id": retry["id"], "attempts": attempts})
task("start", node_id="app-a")

good = lab["draft"]["nodes"][0]["config"]
topology = lab["draft"]
topology["nodes"][0]["config"] = good.replace("proxy_set_header X-Request-ID $http_x_request_id;", 'proxy_set_header X-Request-ID "";')
save(topology)
task("apply")
missing = request()
assert not missing["result"]["trace_complete"]
assert not any(e["source"] == "app" for e in missing["events"])
record("missing_header_incomplete", missing["id"])

# Actual error response from the app, not a synthetic UI error.
topology = lab["draft"]
topology["nodes"][0]["config"] = good
for n in topology["nodes"]:
    if n["kind"] == "app":
        n["error_rate"] = 100
save(topology)
task("apply")
error = request()
assert error["result"]["status_code"] == 503 and "503" in error["result"]["body"]
record("real_503_page", error["id"])

# Nginx-generated error page and >1 MB static fixture, installed only in our gateway.
topology = lab["draft"]
topology["nodes"][0]["config"] = good.replace("listen 8080;", 'listen 8080; location = /nginx-error { return 502; } location = /large { root /tmp; }')
for n in topology["nodes"]:
    if n["kind"] == "app":
        n["error_rate"] = 0
save(topology)
task("apply")
remote(f"from infra.runtime import Runtime,archive; import json; r=Runtime(); c=r.node('{lab_id}','gateway'); c.put_archive('/tmp',archive('large','x'*1100000)); print(json.dumps(True))")
large = request("/large")
assert large["result"]["truncated"] and large["result"]["bytes_captured"] == 1048576
error_page = request("/nginx-error")
assert error_page["result"]["status_code"] == 502 and "nginx" in error_page["result"]["body"]
record("truncation_and_nginx_error_page", {"large": large["id"], "error": error_page["id"]})

topology = lab["draft"]
topology["nodes"][0]["config"] = good.replace("proxy_read_timeout 2s", "proxy_read_timeout 20s")
for n in topology["nodes"]:
    if n["kind"] == "app":
        n["delay_ms"] = 10000
save(topology)
task("apply")
timeout = request()
assert timeout["result"]["error_kind"] == "timeout", timeout
assert any(e["kind"] in ("app.response_written", "app.response_aborted") for e in timeout["events"])
record("client_timeout_separate_from_app_end", {"request_id": timeout["id"], "events": timeout["events"]})

topology = lab["draft"]
topology["nodes"][0]["config"] = good
for n in topology["nodes"]:
    if n["kind"] == "app":
        n["delay_ms"] = 1500
save(topology)
task("apply")
assert request()["result"]["status_code"] == 200
record("healthy_for_browser", lab_id)
