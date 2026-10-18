"""Controlled failures on response_acceptance's dedicated app; restarts its Worker."""
import json
import shlex
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

seed = json.loads(Path("artifacts/response-acceptance.json").read_text(encoding="utf-8"))
lab_id = seed["lab_id"]
root = f"http://127.0.0.1:8088/api/labs/{lab_id}"
container = f"il-{lab_id}-app-a"


def api(path="", method="GET", body=None):
    request = urllib.request.Request(root + path, method=method, data=json.dumps(body).encode() if body else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def ssh(command):
    return subprocess.run(["ssh", "root@188.66.8.20", command], check=True, capture_output=True, text=True).stdout


assert api()["name"] == "应用响应验收"
path = "/nodes/app-a/response"
evidence = {}


def wait(task_id):
    for _ in range(100):
        task = next(t for t in api("/tasks") if t["id"] == task_id)
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(.2)
    raise TimeoutError(task_id)


for interrupt in (False, True):
    saved = api(path)
    old = saved["state"]["applied_version"]
    config = {**saved["config"], "mode": "custom", "format": "text", "body": "中断恢复验证", "status": 200, "delay_ms": 0}
    saved = api(path, "PUT", {"base_version": saved["version"], "config": config})
    version = saved["version"]
    ssh(f"docker kill --signal STOP {container}")
    try:
        task = api("/tasks", "POST", {"kind": "apply_response", "request_id": uuid.uuid4().hex, "revision": api()["revision"], "node_id": "app-a", "response_version": version})
        if interrupt:
            # Wait inside the container for atomic publication, then kill the Worker
            # before the deliberately blocked HTTP health confirmation can finish.
            code = "import json,time\nfor _ in range(1500):\n try:\n  v=json.load(open('/tmp/infra-response.json'))['version']\n except Exception: v=0\n if v==" + str(version) + ": break\n time.sleep(.02)\nelse: raise RuntimeError('publication not seen')"
            ssh(f"docker exec {container} python -c {shlex.quote(code)} && docker restart -t 0 infra-lab-worker-1")
        result = wait(task["id"])
        assert result["status"] == ("interrupted" if interrupt else "failed"), result
    finally:
        ssh(f"docker kill --signal CONT {container}")
    expected = version if interrupt else old
    for _ in range(100):
        state = api(path)["state"]
        if state["observed_version"] == expected and not state["error"]:
            break
        time.sleep(.3)
    else:
        raise AssertionError(state)
    evidence["interruption_reconciled" if interrupt else "failed_probe_rolled_back"] = {"task": result["id"], "status": result["status"], "observed_version": expected}
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
saved = api(path)
saved = api(path, "PUT", {"base_version": saved["version"], "config": {**saved["config"], "mode": "website"}})
task = api("/tasks", "POST", {"kind": "apply_response", "request_id": uuid.uuid4().hex, "revision": api()["revision"], "node_id": "app-a", "response_version": saved["version"]})
assert wait(task["id"])["status"] == "succeeded"
Path("artifacts/response-recovery.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
