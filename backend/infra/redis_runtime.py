"""Bounded Redis operations: argv only, no shell or arbitrary command input."""

import json
import time


def redis_config(node):
    source = node.get("replica_of")
    return (
        "bind 0.0.0.0\nprotected-mode no\nport 6379\ndir /tmp\n"
        "appendonly yes\nappendfsync everysec\nsave \"\"\n"
        "replica-read-only yes\nrepl-diskless-sync yes\nrepl-diskless-sync-delay 0\n"
        f"maxmemory {max(16, node['memory_mb'] // 4)}mb\nmaxmemory-policy noeviction\n"
        + (f"replicaof {source} 6379\n" if source else "")
    )


def cli(container, *args):
    response = container.exec_run(["timeout", "4", "redis-cli", "--json", "-e", *args])
    output = response.output.decode(errors="replace").strip()
    if response.exit_code:
        raise RuntimeError(output or "Redis 操作超时或执行失败")
    # redis-cli intentionally prints INFO as text even with --json.
    if args[0] == "INFO":
        return output
    return json.loads(output)


def replication(container):
    try:
        raw = cli(container, "INFO", "replication")
        fields = dict(line.split(":", 1) for line in raw.splitlines() if ":" in line and not line.startswith("#"))
        return {"available": True, "fields": fields, "observed_at": time.time()}
    except Exception as exc:
        return {"available": False, "error": str(exc), "observed_at": time.time()}


def command(runtime, lab_id, action):
    container = runtime.node(lab_id, action["node_id"])
    container.reload()
    if container.labels.get("infra.kind") != "redis" or container.status != "running":
        raise RuntimeError("Redis 节点未运行或类型不匹配")
    op = action["redis_op"]
    if op not in {"GET", "SET", "DEL", "TTL", "DBSIZE", "PING"}:
        raise RuntimeError("不支持的 Redis 命令")
    args = [op]
    if op in {"GET", "SET", "DEL", "TTL"}:
        args.append(action["key"])
    if op == "SET":
        args.append(action["value"])
        if action.get("ttl"):
            args.extend(["EX", str(action["ttl"])])
    started = time.monotonic()
    value = cli(container, *args)
    return {"node_id": action["node_id"], "operation": op, "value": value,
            "latency_ms": round((time.monotonic() - started) * 1000, 2), "observed_at": time.time()}
