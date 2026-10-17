"""Resume acceptance using only the lab recorded by client_acceptance.py."""
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

lab_id = json.loads(Path("artifacts/client-acceptance.json").read_text(encoding="utf-8"))["lab_id"]
base = "http://127.0.0.1:8088/api"
url = f"/labs/{lab_id}"
evidence = {}


def api(path, method="GET", body=None):
    request = urllib.request.Request(base + path, method=method, headers={"Content-Type": "application/json"}, data=json.dumps(body).encode() if body else None)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def submit(kind, **extra):
    lab = api(url)
    return api(url + "/tasks", "POST", {"kind": kind, "revision": lab["revision"], "request_id": uuid.uuid4().hex, **extra})["id"]


def wait(task_id, expected="succeeded"):
    for _ in range(180):
        task = next(t for t in api(url + "/tasks") if t["id"] == task_id)
        if task["status"] not in ("queued", "running"):
            assert task["status"] == expected, task
            return task
        time.sleep(.3)
    raise TimeoutError(task_id)


# Test the retained container address after Docker has cleared its stopped endpoint.
wait(submit("stop", node_id="app-a"))
request_id = submit("client_request", node_id="gateway", trace=True)
wait(request_id)
detail = api(url + "/requests/" + request_id)
attempts = next(e for e in detail["events"] if e["kind"] == "nginx.completed")["data"]["attempts"]
assert attempts[0]["node_id"] == "app-a" and attempts[0]["mapping"] == "last_observed", attempts
assert attempts[-1]["node_id"] == "app-b"
evidence["retained_address_retry"] = attempts
wait(submit("start", node_id="app-a"))

# Reconnect from an event ID; the first trace returned must be strictly newer.
cursor = detail["events"][0]["seq"]
request = urllib.request.Request(base + url + "/requests/events?after=0", headers={"Last-Event-ID": str(cursor)})
with urllib.request.urlopen(request, timeout=10) as stream:
    while True:
        line = stream.readline().decode()
        if line.startswith("id:"):
            new_cursor = int(line.split(":", 1)[1])
            assert new_cursor > cursor
            evidence["sse_resume"] = {"after": cursor, "first": new_cursor}
            break

# Interrupt only the control Worker while our own single request is processing.
request_id = submit("client_request", node_id="gateway", trace=True)
for _ in range(100):
    detail = api(url + "/requests/" + request_id)
    if any(e["kind"] == "app.processing" for e in detail["events"]):
        break
    time.sleep(.1)
else:
    raise AssertionError("No live processing event")
subprocess.run(["ssh", "-o", "BatchMode=yes", "root@188.66.8.20", "docker restart -t 0 infra-lab-worker-1"], check=True, capture_output=True)
wait(request_id, "interrupted")
detail = api(url + "/requests/" + request_id)
assert detail["status"] == "interrupted"
assert detail["events"][-1]["kind"] == "worker.interrupted"
evidence["worker_interrupted"] = detail
# Do not silently remove failure evidence. Explicitly remove this known test runner only.
code = f"""from infra.runtime import Runtime
r=Runtime()
for c in r.containers('{lab_id}'):
    if c.labels.get('infra.role')=='client' and c.labels.get('infra.node')=='client-{request_id}':
        c.remove(force=True)
"""
subprocess.run(["ssh", "-o", "BatchMode=yes", "root@188.66.8.20", "docker exec -i infra-lab-api-1 python -"], input=code, text=True, check=True)
new_id = submit("client_request", node_id="gateway", trace=True)
wait(new_id)
assert api(url + "/requests/" + new_id)["result"]["status_code"] == 200
assert any(r["id"] == request_id and r["status"] == "interrupted" for r in api(url + "/requests"))
evidence["recovered_request"] = new_id
Path("artifacts/client-recovery.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"checks": list(evidence), "lab_id": lab_id}, ensure_ascii=False))
