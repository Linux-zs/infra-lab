"""Explicit integration recovery check; pass SSH host and deployment path.
Only restarts this project's worker and cleans its own temporary lab.
"""

import argparse
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ssh", required=True)
parser.add_argument("--directory", default="/data/infra-lab")
parser.add_argument("--url", default="http://127.0.0.1:8088")
args = parser.parse_args()
if not args.directory.startswith("/") or any(
    c not in "/-_.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    for c in args.directory
):
    raise ValueError("Use a simple absolute deployment path")


def ssh(command):
    return subprocess.check_output(
        ["ssh", "-o", "BatchMode=yes", args.ssh, command], text=True
    )


def api(path, method="GET", body=None):
    request = urllib.request.Request(
        args.url + "/api" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def task(lab, kind, **extra):
    return api(
        f"/labs/{lab['id']}/tasks",
        "POST",
        {
            "kind": kind,
            "revision": lab["revision"],
            "request_id": uuid.uuid4().hex,
            **extra,
        },
    )


def wait(lab, submitted, statuses, timeout=90):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        current = next(
            t for t in api(f"/labs/{lab['id']}/tasks") if t["id"] == submitted["id"]
        )
        if current["status"] in statuses:
            return current
        if current["status"] == "failed":
            raise RuntimeError(current)
        time.sleep(1)
    raise TimeoutError(submitted)


before = set(ssh("docker ps -q").split())
lab = api("/labs", "POST", {"name": "恢复验收 · 已清理"})
topology = {
    "nodes": [
        {
            "id": "gateway",
            "kind": "nginx",
            "position": {"x": 0, "y": 0},
            "memory_mb": 64,
            "cpu": 0.25,
        },
        {
            "id": "app-1",
            "kind": "app",
            "position": {"x": 300, "y": 0},
            "memory_mb": 64,
            "cpu": 0.25,
            "delay_ms": 150,
        },
    ],
    "edges": [{"source": "gateway", "target": "app-1", "weight": 1}],
}
lab = api(f"/labs/{lab['id']}/draft", "PUT", {"base_revision": 0, "topology": topology})
wait(lab, task(lab, "apply"), {"succeeded"})
load = task(lab, "load", node_id="gateway", count=1000, concurrency=1, duration=60)
wait(lab, load, {"running"})
time.sleep(4)
ssh(
    f"cd {args.directory} && docker compose kill -s SIGKILL worker && docker compose start worker"
)
interrupted = wait(lab, load, {"interrupted"})
assert "中断" in interrupted["logs"][-1]["message"]
wait(lab, task(lab, "cleanup"), {"succeeded"})
after = set(ssh("docker ps -q").split())
# docker compose kill/start preserves the worker container ID.
assert before == after, {
    "before_only": list(before - after),
    "after_only": list(after - before),
}
evidence = {
    "lab_id": lab["id"],
    "interrupted_task": interrupted["id"],
    "status": interrupted["status"],
    "preexisting_running_container_ids_unchanged": True,
    "time": time.time(),
}
Path("artifacts/recovery.json").write_text(
    json.dumps(evidence, indent=2), encoding="utf-8"
)
print(json.dumps(evidence), flush=True)
