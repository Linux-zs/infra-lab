"""Real Redis acceptance through the local SSH management tunnel.

Leaves only its dedicated lab for browser inspection; clean it via the UI after inspection.
"""
import json
import os
import time
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8088/api"
evidence = {}


def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def record(key, value):
    evidence[key] = value
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/redis-acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(key, json.dumps(value, ensure_ascii=False), flush=True)


resume = os.getenv("INFRA_REDIS_ACCEPTANCE_LAB")
lab = api("/labs/" + resume) if resume else api("/labs", "POST", {"name": "Redis 主从验收"})
assert lab["name"] == "Redis 主从验收"
root = "/labs/" + lab["id"]
record("lab_id", lab["id"])
nodes = [{"id": name, "kind": "redis", "position": {"x": 80 if i == 0 else 440, "y": 180 if i == 0 else 40 + (i-1)*220}, "memory_mb": 64, "cpu": .25}
         for i, name in enumerate(["redis-primary", "redis-replica-1", "redis-replica-2"])]
edges = [{"source": "redis-primary", "target": n["id"], "weight": 1} for n in nodes[1:]]
api(root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": {"nodes": nodes, "edges": edges}})


def task(kind, expect="succeeded", **extra):
    body = {"kind": kind, "revision": api(root)["revision"], "request_id": uuid.uuid4().hex, **extra}
    first = api(root + "/tasks", "POST", body)
    assert api(root + "/tasks", "POST", body)["id"] == first["id"]
    for _ in range(240):
        current = next(t for t in api(root + "/tasks") if t["id"] == first["id"])
        if current["status"] not in ("queued", "running"):
            assert current["status"] == expect, current
            return current
        time.sleep(.5)
    raise TimeoutError(first)


def command(node, op, **extra):
    return task("redis_command", node_id=node, redis_op=op, **extra)["result"]["value"]


def wait_observation(predicate):
    for _ in range(100):
        value = api(root).get("observation") or {}
        observed = {n["id"]: n for n in value.get("nodes", [])}
        if predicate(observed):
            return observed
        time.sleep(.5)
    raise AssertionError(observed)


task("apply")
observed = wait_observation(lambda ns: len(ns) == 3 and all(n.get("redis", {}).get("fields", {}).get("master_link_status") == "up" for k, n in ns.items() if k != "redis-primary"))
record("replication_connected", observed)
ids = {k: n["container_id"] for k, n in observed.items()}
task("apply")
assert {k: n["container_id"] for k, n in wait_observation(lambda ns: len(ns) == 3).items()} == ids
record("repeat_apply_keeps_containers", ids)
assert command("redis-primary", "SET", key="demo", value="真实复制") == "OK"
for name in ids:
    for _ in range(10):
        result = command(name, "GET", key="demo")
        if result == "真实复制":
            break
        time.sleep(.2)
    assert result == "真实复制"
record("primary_write_replicated", True)
failed = task("redis_command", expect="failed", node_id="redis-replica-1", redis_op="SET", key="demo", value="blocked")
assert "READONLY" in failed["result"]["error"]
record("replica_write_rejected", failed["result"])
assert command("redis-primary", "GET", key="missing") is None
assert command("redis-primary", "SET", key="expires", value="v", ttl=2) == "OK"
time.sleep(2.2)
assert command("redis-replica-2", "GET", key="expires") is None
record("missing_and_expiration", True)
task("stop", node_id="redis-primary")
wait_observation(lambda ns: ns.get("redis-replica-1", {}).get("redis", {}).get("fields", {}).get("master_link_status") == "down")
assert command("redis-replica-1", "GET", key="demo") == "真实复制"
record("stopped_primary_replica_retains_data_no_promotion", True)
task("start", node_id="redis-primary")
wait_observation(lambda ns: ns.get("redis-replica-1", {}).get("redis", {}).get("fields", {}).get("master_link_status") == "up")
assert command("redis-primary", "GET", key="demo") == "真实复制"
task("stop_lab")
task("start_lab")
assert command("redis-primary", "GET", key="demo") == "真实复制"
record("aof_survives_lab_restart", True)
# Detach one replica without recreating it; role is an observed server fact.
lab = api(root)
topology = lab["draft"]
topology["edges"] = topology["edges"][:1]
api(root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": topology})
task("apply")
observed = wait_observation(lambda ns: ns.get("redis-replica-2", {}).get("redis", {}).get("fields", {}).get("role") == "master")
assert observed["redis-replica-2"]["container_id"] == ids["redis-replica-2"]
assert command("redis-replica-2", "GET", key="demo") == "真实复制"
record("detach_without_recreate", True)
lab = api(root)
topology["edges"] = edges
api(root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": topology})
task("apply")
wait_observation(lambda ns: ns.get("redis-replica-2", {}).get("redis", {}).get("fields", {}).get("master_link_status") == "up")
record("ready_for_browser", lab["id"])
