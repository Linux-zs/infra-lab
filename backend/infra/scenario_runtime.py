"""Scenario recipes execute inside the normal per-lab Worker queue."""

import json
import time

from . import db
from .response_runtime import apply_response


def snapshot(runtime, lab_id):
    observation = runtime.observe(lab_id)
    with db.connect() as con:
        lab = db.unpack(con.execute("SELECT * FROM labs WHERE id=?", (lab_id,)).fetchone())
    return {
        "collected_at": db.now(),
        "applied_revision": lab["applied_revision"],
        "topology": lab["applied"],
        "observation": observation,
    }


def execute_step(task, runtime, emit):
    lab_id, task_id = task["lab_id"], task["id"]
    action, plan = task["payload"]["action"], task["payload"]["scenario_plan"]
    with db.connect() as con:
        saved_snapshot = db.unpack(
            con.execute("SELECT snapshot FROM scenario_runs WHERE task_id=?", (task_id,)).fetchone()
        )["snapshot"]
    result = {"completed_actions": [], "measure": plan["measure"]}

    def persist():
        with db.connect() as con:
            con.execute(
                "UPDATE scenario_runs SET snapshot=?,result=? WHERE task_id=?",
                (json.dumps(saved_snapshot), json.dumps(result), task_id),
            )

    def completed(name):
        result["completed_actions"].append({"action": name, "time": db.now()})
        persist()

    saved_snapshot["before"] = snapshot(runtime, lab_id)
    persist()
    for node_id, operation in plan.get("lifecycle", {}).items():
        container = runtime.node(lab_id, node_id)
        container.reload()
        if operation == "start":
            if container.status != "running":
                container.start()
            runtime.ready(container)
        elif operation == "stop":
            if container.status == "running":
                container.stop(timeout=3)
        else:
            raise RuntimeError("未知场景启停操作")
        emit(f"{node_id} {operation} 已执行")
        completed(f"{node_id}:{operation}")
    if plan.get("apply_topology"):
        runtime.apply(lab_id, plan["apply_topology"], task_id, emit)
        with db.connect() as con:
            con.execute(
                "UPDATE labs SET applied=?,applied_revision=? WHERE id=?",
                (json.dumps(plan["apply_topology"]), plan["revision"], lab_id),
            )
        completed("apply_topology")
    for node_id, response in plan.get("responses", {}).items():
        apply_response(runtime, lab_id, node_id, response["version"], response["config"], emit)
        completed(f"{node_id}:response-v{response['version']}")
    if plan.get("reload"):
        gateway = runtime.node(lab_id, "gateway")
        current = gateway.exec_run(["cat", "/etc/nginx/nginx.conf"])
        if current.exit_code:
            raise RuntimeError("无法读取运行 Nginx 配置")
        runtime.reload(gateway, current.output.decode(), emit)
        completed("reload")

    actual_action = {**action, "node_id": "gateway", "kind": plan["measure"], "path": "/page"}
    if plan["measure"] == "load":
        raw = runtime.load(lab_id, actual_action, task_id, emit)
        result["load"] = {
            k: raw.get(k)
            for k in (
                "requested",
                "identified",
                "unidentified",
                "completed",
                "errors",
                "error_rate",
                "p95_ms",
                "distribution",
                "upstream_logs",
                "runtime_config",
                "runtime_nodes",
            )
        }
    elif plan["measure"] == "request":
        db.request_status(task_id, "running")
        raw = runtime.client_request(lab_id, actual_action, task_id, emit)
        with db.connect() as con:
            revision = con.execute("SELECT applied_revision FROM labs WHERE id=?", (lab_id,)).fetchone()[0]
        raw["applied_revision"] = revision
        db.request_status(task_id, "completed", raw)
        with db.connect() as con:
            events = [
                db.unpack(r)
                for r in con.execute(
                    "SELECT * FROM request_events WHERE request_id=? ORDER BY seq", (task_id,)
                )
            ]
        headers = {k.lower(): v for k, v in raw.get("headers", [])}
        result["request"] = {
            "request_id": task_id,
            **{
                k: raw.get(k)
                for k in (
                    "status_code",
                    "latency_ms",
                    "error",
                    "error_kind",
                    "trace_complete",
                    "trace_note",
                    "bytes_captured",
                    "truncated",
                    "runtime_config",
                )
            },
            "node": headers.get("x-infra-node"),
            "response_version": headers.get("x-infra-response-version"),
            "events": events,
        }
    elif plan["measure"] == "redis":
        key = f"scenario:{lab_id}"
        result["commands"] = []

        def command(node_id, operation, expected_error=None):
            params = {**action, "node_id": node_id, "redis_op": operation, "key": key}
            try:
                value = runtime.redis_command(lab_id, params)
            except RuntimeError as exc:
                if not expected_error or expected_error not in str(exc):
                    raise
                value = {
                    "node_id": node_id,
                    "operation": operation,
                    "error": str(exc),
                    "expected_error": True,
                    "observed_at": db.now(),
                }
            else:
                if expected_error:
                    value["expected_error"] = False
            result["commands"].append({**value, "key": key})
            persist()

        operation = plan["redis"]
        if operation == "replicate":
            command("redis-primary", "SET")
        if operation == "readonly":
            command("redis-replica-1", "SET", "READONLY")
        else:
            for name in (("redis-primary",) if operation != "read" else ()) + (
                "redis-replica-1",
                "redis-replica-2",
            ):
                command(name, "GET")
    else:
        raise RuntimeError("未知场景测量类型")
    completed("measure")
    # Capture actual state after measurements; Redis links may need time to reconnect.
    if plan.get("redis") == "read-all":
        deadline = time.monotonic() + 15
        while True:
            current = runtime.observe(lab_id)
            if (
                all(
                    n.get("redis", {}).get("fields", {}).get("master_link_status") == "up"
                    for n in current.get("nodes", [])
                    if n["id"] != "redis-primary"
                )
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.25)
    saved_snapshot["after"] = snapshot(runtime, lab_id)
    persist()
    return result
