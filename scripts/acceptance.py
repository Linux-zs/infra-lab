"""Real-container acceptance. Run against an explicitly selected local/tunnel URL.
Creates its own labelled lab; leaves its final healthy topology for inspection.
"""

import argparse
import json
import time
import urllib.request
import uuid
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:8088")
parser.add_argument("--output", default="artifacts/acceptance.json")
args = parser.parse_args()


def api(path, method="GET", body=None):
    request = urllib.request.Request(
        args.url + "/api" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


lab = api("/labs", "POST", {"name": "Nginx 真实分流实验"})
lab_id = lab["id"]
evidence = {"lab_id": lab_id, "checks": []}


def record(name, value):
    evidence["checks"].append({"name": name, "time": time.time(), "value": value})
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(name + ": " + json.dumps(value, ensure_ascii=False), flush=True)


def save(topology):
    global lab
    lab = api(
        f"/labs/{lab_id}/draft",
        "PUT",
        {"base_revision": lab["revision"], "topology": topology},
    )


def task(kind, expect="succeeded", **extra):
    action = {
        "kind": kind,
        "revision": lab["revision"],
        "request_id": uuid.uuid4().hex,
        **extra,
    }
    submitted = api(f"/labs/{lab_id}/tasks", "POST", action)
    duplicate = api(f"/labs/{lab_id}/tasks", "POST", action)
    assert submitted["id"] == duplicate["id"], "重复提交产生了新任务"
    start = time.monotonic()
    while time.monotonic() - start < 120:
        current = next(
            t for t in api(f"/labs/{lab_id}/tasks") if t["id"] == submitted["id"]
        )
        if current["status"] not in ("queued", "running"):
            assert current["status"] == expect, json.dumps(current, ensure_ascii=False)
            return current
        time.sleep(1)
    raise TimeoutError(submitted["id"])


nodes = [
    {
        "id": "gateway",
        "kind": "nginx",
        "position": {"x": 40, "y": 210},
        "memory_mb": 64,
        "cpu": 0.25,
    }
]
nodes += [
    {
        "id": f"app-{i + 1}",
        "kind": "app",
        "position": {"x": 390, "y": i * 180 + 30},
        "memory_mb": 64,
        "cpu": 0.25,
    }
    for i in range(3)
]
topology = {
    "nodes": nodes,
    "edges": [
        {"source": "gateway", "target": f"app-{i + 1}", "weight": 1} for i in range(3)
    ],
}
save(topology)
task("apply")
record("create_1_nginx_3_apps", {"revision": lab["revision"]})
baseline = task("load", node_id="gateway", count=120, concurrency=2, duration=30)[
    "result"
]
assert baseline["completed"] == 120 and baseline["errors"] == 0, baseline
assert set(baseline["distribution"]) == {"app-1", "app-2", "app-3"}, baseline
record(
    "equal_weight",
    {k: baseline[k] for k in ("completed", "errors", "distribution", "p95_ms")},
)

topology = lab["draft"]
topology["edges"][0]["weight"] = 3
save(topology)
task("apply")
weighted = task("load", node_id="gateway", count=150, concurrency=2, duration=30)[
    "result"
]
assert weighted["errors"] == 0
assert weighted["distribution"]["app-1"] > weighted["distribution"]["app-2"] * 2
record(
    "weight_3_1_1", {k: weighted[k] for k in ("completed", "errors", "distribution")}
)

good_config = lab["draft"]["nodes"][0]["config"]
good_revision = lab["revision"]
topology = lab["draft"]
topology["nodes"][0].update(mode="manual", config="not_a_valid_directive;\n")
save(topology)
failed = task("apply", expect="failed")
assert api(f"/labs/{lab_id}")["applied_revision"] == good_revision
after_bad = task("load", node_id="gateway", count=30, concurrency=2, duration=15)[
    "result"
]
assert after_bad["errors"] == 0
record(
    "invalid_config_preserves_running",
    {"error": failed["result"]["error"], "successful_requests": after_bad["completed"]},
)

topology = lab["draft"]
topology["nodes"][0].update(mode="generated", config=good_config)
save(topology)
task("apply")
task("stop", node_id="app-1")
stopped = task("load", node_id="gateway", count=30, concurrency=2, duration=20)[
    "result"
]
assert "app-1" not in stopped["distribution"] and stopped["errors"] == 0
record(
    "stop_and_retry",
    {
        "distribution": stopped["distribution"],
        "errors": stopped["errors"],
        "upstream_logs": stopped["upstream_logs"][:6],
    },
)
task("start", node_id="app-1")

topology = lab["draft"]
topology["nodes"][1]["delay_ms"] = 2600
save(topology)
task("apply")
slow = task("load", node_id="gateway", count=12, concurrency=2, duration=25)["result"]
assert any("," in entry.get("upstream", "") for entry in slow["upstream_logs"]), slow
record(
    "slow_backend_timeout_retry",
    {
        "completed": slow["completed"],
        "errors": slow["errors"],
        "p95_ms": slow["p95_ms"],
        "upstream_logs": slow["upstream_logs"][:6],
    },
)
topology = lab["draft"]
topology["nodes"][1]["delay_ms"] = 0
save(topology)
task("apply")

task("cleanup")
time.sleep(3)
clean = api(f"/labs/{lab_id}")
assert clean["applied"] is None and clean["observation"]["nodes"] == [], clean
record("cleanup_only_lab_resources", {"remaining_nodes": clean["observation"]["nodes"]})
task("apply")
final = task("load", node_id="gateway", count=120, concurrency=2, duration=30)["result"]
assert final["errors"] == 0
record(
    "recreate_and_final_demo",
    {"distribution": final["distribution"], "errors": final["errors"]},
)
print("PASS: " + lab_id, flush=True)
