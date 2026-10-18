"""Docker adapter. Only resources carrying both ownership labels are mutable."""

import hashlib
import io
import json
import os
import tarfile
import threading
import time

import docker
from docker.errors import NotFound

from .redis_runtime import cli, command, redis_config, replication

OWNER = "infra-lab-v1"
NODE_IP_PREFIX = "node-ip-v2"
RESOURCE_LOCK = threading.RLock()


def labels(lab_id, **extra):
    return {"infra.owner": OWNER, "infra.lab": lab_id, **extra}


def filters(lab_id):
    return {"label": [f"infra.owner={OWNER}", f"infra.lab={lab_id}"]}


def node_ip_key(lab_id, container_id):
    return f"{NODE_IP_PREFIX}:{lab_id}:{container_id}"


def container_memory(container):
    return int(container.attrs.get("HostConfig", {}).get("Memory", 0))


def projected_peak_memory(lab_containers, nodes, image_for):
    """Conservative same-lab peak while replacing nodes and validating Nginx."""
    resident = sum(container_memory(c) for c in lab_containers)
    peak = resident
    existing = {
        c.labels.get("infra.node"): c
        for c in lab_containers
        if c.labels.get("infra.role") == "node"
    }
    by_id = {node["id"]: node for node in nodes}

    def rank(node):
        if node["kind"] == "nginx":
            return 100
        depth, parent = 0, node.get("replica_of")
        while parent:
            depth += 1
            parent = by_id[parent].get("replica_of")
        return depth

    for node in sorted(nodes, key=rank):
        memory = int(node["memory_mb"]) * 1048576
        current = existing.get(node["id"])
        if node["kind"] == "nginx":
            peak = max(peak, resident + memory)
        replace = bool(
            current
            and (
                current.labels.get("infra.spec") != fingerprint(node)
                or current.attrs.get("Config", {}).get("Image") != image_for(node["kind"])
                or (
                    node["kind"] == "app"
                    and current.labels.get("infra.capabilities") != "client-page-trace-v3"
                )
            )
        )
        if replace:
            resident -= container_memory(current)
            current = None
        if current is None:
            resident += memory
        peak = max(peak, resident)
    return peak


def fingerprint(node):
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in node.items() if k not in ("position", "config", "mode", "replica_of")}, sort_keys=True
        ).encode()
    ).hexdigest()[:16]


def archive(path, content, uid=0):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        data = content.encode()
        info = tarfile.TarInfo(path)
        info.size = len(data)
        info.mode = 0o644
        info.uid = info.gid = uid
        tar.addfile(info, io.BytesIO(data))
    return output.getvalue()


class Runtime:
    def __init__(self):
        self.client = docker.from_env(timeout=15)
        self.client.ping()
        self.nginx_image = os.getenv("INFRA_NGINX_IMAGE", "nginx:1.28.0-alpine")
        self.redis_image = os.getenv("INFRA_REDIS_IMAGE", "redis:7.4.7-alpine")
        self.app_image = os.getenv("INFRA_APP_IMAGE", "infra-lab-app:1.3.0")

    def image_for(self, kind):
        return {"nginx": self.nginx_image, "app": self.app_image, "redis": self.redis_image}[kind]

    def redis_command(self, lab_id, action):
        return command(self, lab_id, action)

    def close(self):
        self.client.close()

    def containers(self, lab_id):
        return self.client.containers.list(all=True, filters=filters(lab_id), ignore_removed=True)

    def node(self, lab_id, node_id):
        found = [
            c
            for c in self.containers(lab_id)
            if c.labels.get("infra.node") == node_id and c.labels.get("infra.role") == "node"
        ]
        if len(found) != 1:
            raise RuntimeError(f"节点 {node_id} 对应 {len(found)} 个容器，请核对运行状态")
        return found[0]

    def network(self, lab_id):
        name = f"il-{lab_id}"
        try:
            net = self.client.networks.get(name)
            if any(net.attrs.get("Labels", {}).get(k) != v for k, v in labels(lab_id).items()):
                raise RuntimeError("网络名称与非本项目资源冲突")
            return net
        except NotFound:
            return self.client.networks.create(name, internal=True, labels=labels(lab_id))

    def ensure_image(self, image):
        try:
            self.client.images.get(image)
        except NotFound:
            if image == self.app_image:
                raise RuntimeError("测试应用镜像不存在，请先运行部署构建") from None
            self.client.images.pull(image)

    def create(self, lab_id, node, name=None, role="node", command=None):
        image = self.image_for(node["kind"])
        self.ensure_image(image)
        network = self.network(lab_id)
        options = dict(
            image=image,
            name=name or f"il-{lab_id}-{node['id']}",
            network=network.name,
            detach=True,
            mem_limit=f"{node['memory_mb']}m",
            nano_cpus=int(node["cpu"] * 1e9),
            pids_limit=96,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            labels=labels(
                lab_id,
                **{
                    "infra.node": node["id"],
                    "infra.role": role,
                    "infra.spec": fingerprint(node),
                    "infra.kind": node["kind"],
                    "infra.capabilities": "client-page-trace-v3" if node["kind"] == "app" else "",
                },
            ),
            log_config=docker.types.LogConfig(type="json-file", config={"max-size": "5m", "max-file": "2"}),
        )
        if node["kind"] == "nginx":
            options.update(
                user="101:101",
                entrypoint=["nginx"],
                command=command or ["-g", "daemon off;"],
                healthcheck={
                    "test": ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:8080/__infra_health"],
                    "interval": 5_000_000_000,
                    "timeout": 2_000_000_000,
                    "retries": 2,
                    "start_period": 3_000_000_000,
                },
            )
        elif node["kind"] == "redis":
            options.update(
                user="999:999", entrypoint=["redis-server"], command=["/tmp/redis.conf"],
                working_dir="/tmp", tmpfs={"/data": "rw,noexec,nosuid,size=1m"},
                healthcheck={"test": ["CMD", "redis-cli", "ping"], "interval": 5_000_000_000,
                             "timeout": 2_000_000_000, "retries": 2, "start_period": 3_000_000_000},
            )
        else:
            options.update(
                environment={
                    "NODE_ID": node["id"],
                    "DELAY_MS": str(node["delay_ms"]),
                    "ERROR_RATE": str(node["error_rate"]),
                    "ERROR_STATUS": str(node["error_status"]),
                }
            )
            if command:
                options["command"] = command
        with RESOURCE_LOCK:
            container = self.client.containers.create(**options)
        if role == "node":
            # Docker's default alias is the long container name; add stable per-lab DNS.
            network.disconnect(container)
            network.connect(container, aliases=[node["id"]])
        if node["kind"] == "nginx":
            container.put_archive("/etc/nginx", archive("nginx.conf", node["config"]))
        if node["kind"] == "redis":
            container.put_archive("/tmp", archive("redis.conf", redis_config(node)))
        if node["kind"] == "app" and role == "node":
            from . import db
            from .response_runtime import envelope
            with db.connect() as con:
                row = con.execute("SELECT v.version,v.config FROM response_state s JOIN response_versions v ON v.lab_id=s.lab_id AND v.node_id=s.node_id AND v.version=s.applied_version WHERE s.lab_id=? AND s.node_id=?", (lab_id, node["id"])).fetchone()
            if row:
                container.put_archive("/tmp", archive("infra-response.json", json.dumps(envelope(row[0], json.loads(row[1]))), uid=65534))
        return container

    def validate(self, lab_id, node, task_id, emit):
        candidate = self.create(
            lab_id, node, name=f"il-{lab_id}-check-{task_id}-{node['id']}", role="validator", command=["-t"]
        )
        try:
            candidate.start()
            code = candidate.wait(timeout=20)["StatusCode"]
            output = candidate.logs().decode(errors="replace")
            emit(f"{node['id']} nginx -t: {output}")
            if code:
                raise RuntimeError(f"配置校验失败，运行配置未替换：{output}")
        finally:
            candidate.remove(force=True)

    def ready(self, container):
        for _ in range(30):
            container.reload()
            if container.status in ("exited", "dead"):
                raise RuntimeError(container.logs(tail=30).decode(errors="replace"))
            if container.attrs["State"].get("Health", {}).get("Status") == "healthy":
                return
            time.sleep(0.5)
        raise RuntimeError("服务健康检查未通过；容器保留以供排查")

    def reload(self, container, config, emit):
        old = container.exec_run(["cat", "/etc/nginx/nginx.conf"]).output.decode()
        children = ["sh", "-c", "cat /proc/$(cat /tmp/nginx.pid)/task/$(cat /tmp/nginx.pid)/children"]
        before = container.exec_run(children).output.strip()
        container.put_archive("/etc/nginx", archive("nginx.conf", config))
        try:
            check = container.exec_run(["nginx", "-t"])
            emit(check.output.decode(errors="replace"))
            if check.exit_code:
                raise RuntimeError("运行容器内 nginx -t 失败")
            result = container.exec_run(["nginx", "-s", "reload"])
            if result.exit_code:
                raise RuntimeError(result.output.decode(errors="replace"))
            for _ in range(20):
                after = container.exec_run(children).output.strip()
                if after and set(after.split()) - set(before.split()):
                    return
                time.sleep(0.25)
            raise RuntimeError("未确认 Nginx 新 worker 启动")
        except Exception:
            container.put_archive("/etc/nginx", archive("nginx.conf", old))
            container.exec_run(["nginx", "-s", "reload"])
            raise

    def apply(self, lab_id, topology, task_id, emit):
        with RESOURCE_LOCK:
            return self._apply_locked(lab_id, topology, task_id, emit)

    def _apply_locked(self, lab_id, topology, task_id, emit):
        nodes = topology["nodes"]
        owned = self.client.containers.list(
            all=True, filters={"label": f"infra.owner={OWNER}"}, ignore_removed=True
        )
        allocated = sum(container_memory(c) for c in owned if c.labels.get("infra.lab") != lab_id)
        lab_containers = [c for c in owned if c.labels.get("infra.lab") == lab_id]
        limit = int(os.getenv("INFRA_TOTAL_MEMORY_MB", "768")) * 1048576
        peak = projected_peak_memory(lab_containers, nodes, self.image_for)
        if allocated + peak > limit:
            raise RuntimeError("超出平台实验内存配额（默认合计 768 MB），请先清理不用的实验")
        existing = {
            c.labels["infra.node"]: c for c in self.containers(lab_id) if c.labels.get("infra.role") == "node"
        }
        # Backends must exist for nginx -t to resolve upstream names.
        # Redis sources precede replicas; topology validation already rejects cycles.
        by_id = {n["id"]: n for n in nodes}
        def rank(node):
            if node["kind"] == "nginx":
                return 100
            depth, parent = 0, node.get("replica_of")
            while parent:
                depth += 1
                parent = by_id[parent].get("replica_of")
            return depth
        for node in sorted(nodes, key=rank):
            current = existing.get(node["id"])
            if node["kind"] == "nginx":
                self.validate(lab_id, node, task_id, emit)
            expected_image = self.image_for(node["kind"])
            if current and (
                current.labels.get("infra.spec") != fingerprint(node)
                or current.attrs["Config"]["Image"] != expected_image
                or (
                    node["kind"] == "app"
                    and current.labels.get("infra.capabilities") != "client-page-trace-v3"
                )
            ):
                emit(f"{node['id']} 参数改变，重建此实例")
                current.remove(force=True)
                self.forget_container_history(lab_id, current)
                current = None
            if current is None:
                emit(f"创建 {node['id']}")
                current = self.create(lab_id, node)
                current.start()
            else:
                current.reload()
                if current.status != "running":
                    if node["kind"] == "nginx":
                        current.put_archive("/etc/nginx", archive("nginx.conf", node["config"]))
                    current.start()
                elif node["kind"] == "nginx":
                    self.reload(current, node["config"], emit)
            self.ready(current)
            if node["kind"] == "redis":
                previous = current.exec_run(["cat", "/tmp/redis.conf"]).output.decode()
                current.put_archive("/tmp", archive("redis.conf", redis_config(node)))
                try:
                    source = node.get("replica_of")
                    fields = replication(current)
                    if not fields["available"]:
                        raise RuntimeError(fields["error"])
                    actual = fields["fields"]
                    if source and (actual.get("role") != "slave" or actual.get("master_host") != source):
                        cli(current, "REPLICAOF", source, "6379")
                    elif not source and actual.get("role") != "master":
                        cli(current, "REPLICAOF", "NO", "ONE")
                    emit(f"{node['id']} 复制配置已设置，实际同步状态见观测")
                except Exception:
                    current.put_archive("/tmp", archive("redis.conf", previous))
                    raise
            emit(f"{node['id']} 健康检查通过")
        keep = {n["id"] for n in nodes}
        for node_id, container in existing.items():
            if node_id not in keep:
                emit(f"移除已从拓扑删除的节点 {node_id}")
                container.remove(force=True)
                self.forget_container_history(lab_id, container)

    @staticmethod
    def forget_container_history(lab_id, container):
        from . import db

        with db.connect() as con:
            con.execute(
                "DELETE FROM meta WHERE key IN (?,?)",
                (node_ip_key(lab_id, container.id), f"node-ip:{container.id}"),
            )

    def observe(self, lab_id):
        from . import db

        result = []
        nodes = [c for c in self.containers(lab_id) if c.labels.get("infra.role") == "node"]
        keys = [node_ip_key(lab_id, c.id) for c in nodes]
        legacy_keys = [f"node-ip:{c.id}" for c in nodes]
        current = {}
        for container in nodes:
            container.reload()
            current[container.id] = (
                container.attrs["State"],
                container.attrs["NetworkSettings"]["Networks"].get(f"il-{lab_id}", {}).get("IPAddress"),
            )
        history = {}
        if keys:
            with db.connect() as con:
                history = {
                    row[0]: json.loads(row[1])
                    for row in con.execute(
                        "SELECT key,value FROM meta WHERE key IN (" + ",".join("?" for _ in keys + legacy_keys) + ")",
                        keys + legacy_keys,
                    )
                }
                for container in nodes:
                    key = node_ip_key(lab_id, container.id)
                    legacy_key = f"node-ip:{container.id}"
                    ip = current[container.id][1]
                    if ip:
                        history[key] = {"ip": ip, "time": time.time()}
                        con.execute(
                            "INSERT OR REPLACE INTO meta VALUES(?,?)",
                            (key, json.dumps(history[key])),
                        )
                    elif key not in history and legacy_key in history:
                        history[key] = history[legacy_key]
                        con.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, json.dumps(history[key])))
                    if legacy_key in history:
                        con.execute("DELETE FROM meta WHERE key=?", (legacy_key,))
        for c in nodes:
            state, ip = current[c.id]
            key = node_ip_key(lab_id, c.id)
            last_ip = history.get(key)
            response_state = None
            if c.labels.get("infra.capabilities") == "client-page-trace-v3" and c.status == "running":
                from .response_runtime import reconcile
                response_state = reconcile(lab_id, c.labels["infra.node"], c)
            stats = c.stats(stream=False, one_shot=True) if c.status == "running" else {}
            result.append(
                {
                    "id": c.labels["infra.node"],
                    "kind": c.labels["infra.kind"],
                    "container_id": c.short_id,
                    "status": c.status,
                    "health": state.get("Health", {}).get("Status", "unknown"),
                    "memory_bytes": stats.get("memory_stats", {}).get("usage"),
                    "ip": ip,
                    "last_ip": last_ip,
                    "spec": c.labels.get("infra.spec"),
                    "image": c.attrs["Config"]["Image"],
                    "client_trace": c.labels.get("infra.capabilities")
                    in ("client-page-trace-v1", "client-page-trace-v2", "client-page-trace-v3"),
                    "browser_site": c.labels.get("infra.capabilities") in ("client-page-trace-v2", "client-page-trace-v3"),
                    "custom_response": c.labels.get("infra.capabilities") == "client-page-trace-v3",
                    "response": response_state,
                    "redis": replication(c) if c.labels.get("infra.kind") == "redis" and c.status == "running" else None,
                    "observed_at": time.time(),
                }
            )
        return {"available": True, "nodes": result}

    def migrate_node_ip_history(self):
        from . import db

        containers = self.client.containers.list(
            all=True, filters={"label": f"infra.owner={OWNER}"}, ignore_removed=True
        )
        owned = {
            c.id: (c.labels.get("infra.lab"), c.labels.get("infra.role"))
            for c in containers
            if c.labels.get("infra.lab")
        }
        with db.connect() as con:
            for row in con.execute("SELECT key,value FROM meta WHERE key LIKE 'node-ip:%'").fetchall():
                container_id = row["key"][len("node-ip:"):]
                lab_id, role = owned.get(container_id, (None, None))
                if lab_id and role == "node":
                    con.execute(
                        "INSERT OR IGNORE INTO meta VALUES(?,?)",
                        (node_ip_key(lab_id, container_id), row["value"]),
                    )
                con.execute("DELETE FROM meta WHERE key=?", (row["key"],))

    def logs(self, lab_id, node_id):
        return self.node(lab_id, node_id).logs(tail=150).decode(errors="replace")

    def cleanup(self, lab_id, emit):
        for container in self.containers(lab_id):
            emit(f"删除本实验容器 {container.name}")
            container.remove(force=True)
            self.forget_container_history(lab_id, container)
        for network in self.client.networks.list(filters=filters(lab_id)):
            network.remove()

    def lifecycle(self, lab_id, start, emit):
        containers = [c for c in self.containers(lab_id) if c.labels.get("infra.role") == "node"]
        if not containers:
            raise RuntimeError("没有已部署容器，请先应用拓扑")
        results = []
        for container in sorted(containers, key=lambda c: (c.labels.get("infra.kind") == "nginx") == start):
            entry = {"node_id": container.labels["infra.node"]}
            try:
                container.reload()
                if start:
                    skipped = container.status == "running"
                    if not skipped:
                        container.start()
                    self.ready(container)
                else:
                    skipped = container.status in ("exited", "created", "dead")
                    if not skipped:
                        container.stop(timeout=3)
                entry.update(status="skipped" if skipped else "succeeded")
            except Exception as exc:
                entry.update(status="failed", error=str(exc))
            emit(f"{entry['node_id']}: {entry['status']} {entry.get('error', '')}")
            results.append(entry)
        return {"nodes": results, "partial_failure": any(r["status"] == "failed" for r in results)}

    def client_request(self, lab_id, action, task_id, emit):
        from . import db

        target = self.node(lab_id, action["node_id"])
        snapshot = self.observe(lab_id)
        config = (
            target.exec_run(["cat", "/etc/nginx/nginx.conf"]).output.decode(errors="replace")
            if target.status == "running"
            else None
        )
        nodes = [c for c in self.containers(lab_id) if c.labels.get("infra.role") == "node"]
        node = {
            "id": f"client-{task_id}",
            "kind": "app",
            "memory_mb": 64,
            "cpu": 0.25,
            "delay_ms": 0,
            "error_rate": 0,
            "error_status": 503,
        }
        runner = self.create(
            lab_id,
            node,
            role="client",
            command=[
                "python",
                "-u",
                "/app/client.py",
                action["node_id"],
                action["path"],
                task_id,
                "1" if action["trace"] else "0",
            ],
        )
        started = int(time.time())
        seen = set()
        received, finished = set(), set()
        nginx_seen = False
        sources = [(runner, "client")] + [(c, c.labels["infra.kind"]) for c in nodes]

        def collect():
            nonlocal nginx_seen
            if not action["trace"]:
                return
            for container, source in sources:
                for line in container.logs(since=started, tail=2000).decode(errors="replace").splitlines():
                    key = (container.id, line)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("request_id") != task_id:
                        continue
                    node_id = container.labels.get("infra.node") if source != "client" else None
                    if source == "nginx" and "upstream" in event:
                        nginx_seen = True
                        historical = {
                            n["last_ip"]["ip"]: n["id"] for n in snapshot["nodes"] if n.get("last_ip")
                        }
                        ips = {n["ip"]: n["id"] for n in snapshot["nodes"] if n.get("ip")}
                        addresses = str(event["upstream"]).split(",")
                        statuses = str(event.get("upstream_status", "")).split(",")
                        event["attempts"] = [
                            {
                                "address": address.strip(),
                                "node_id": ips.get(address.strip().rsplit(":", 1)[0])
                                or historical.get(address.strip().rsplit(":", 1)[0]),
                                "mapping": "current"
                                if address.strip().rsplit(":", 1)[0] in ips
                                else "last_observed",
                                "status": statuses[i].strip() if i < len(statuses) else "unknown",
                            }
                            for i, address in enumerate(addresses)
                            if address.strip() not in ("", "-")
                        ]
                        db.request_event(task_id, source, "nginx.completed", node_id, "posthoc", data=event)
                    elif event.get("infra_trace") == 1 and source in ("client", "app"):
                        kind = event.get("kind", "unknown")
                        attempt = (node_id, event.get("attempt_id"))
                        if kind == "app.received":
                            received.add(attempt)
                        if kind in ("app.response_written", "app.response_aborted"):
                            finished.add(attempt)
                        db.request_event(
                            task_id, source, kind, node_id, timestamp=event.get("time"), data=event
                        )

        try:
            runner.start()
            emit("单次 GET 已启动；响应与预览共用同一份采集结果")
            deadline = time.monotonic() + 25
            ended_at = None
            while time.monotonic() < deadline:
                collect()
                runner.reload()
                if runner.status in ("exited", "dead"):
                    ended_at = ended_at or time.monotonic()
                    if not action["trace"] or (time.monotonic() - ended_at > 1 and received <= finished):
                        break
                time.sleep(0.2)
            collect()
            if runner.status not in ("exited", "dead"):
                raise RuntimeError("请求发生器异常：超过执行时限，保留明确失败状态")
            stream, _ = runner.get_archive("/tmp/response.json")
            with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
                result = json.load(tar.extractfile(tar.getmembers()[0]))
            complete = nginx_seen and bool(received) and received <= finished
            result.update(
                runtime_nodes=snapshot,
                runtime_config=config,
                trace_complete=complete if action["trace"] else None,
                trace_note=(
                    "日志已关联；路径为事后还原"
                    if complete
                    else "链路不完整：缺少 Nginx／应用事件，或应用尚未确认结束；未观测到的路径保持未知"
                )
                if action["trace"]
                else "未采集详细链路",
            )
            return result
        finally:
            runner.remove(force=True)

    def load(self, lab_id, action, task_id, emit):
        target = self.node(lab_id, action["node_id"])
        target.reload()
        if target.status != "running":
            raise RuntimeError("目标 Nginx 未运行")
        actual_config = target.exec_run(["cat", "/etc/nginx/nginx.conf"])
        if actual_config.exit_code:
            raise RuntimeError("无法读取本次实验运行配置")
        actual_nodes = self.observe(lab_id)
        node = {
            "id": f"load-{task_id}",
            "kind": "app",
            "memory_mb": 64,
            "cpu": 0.25,
            "delay_ms": 0,
            "error_rate": 0,
            "error_status": 503,
        }
        runner = self.create(
            lab_id,
            node,
            role="load",
            command=[
                "python",
                "/app/load.py",
                action["node_id"],
                str(action["count"]),
                str(action["concurrency"]),
                str(action["duration"]),
                task_id,
            ],
        )
        started = int(time.time())
        try:
            runner.start()
            emit("请求发生器已在实验网络启动")
            code = runner.wait(timeout=action["duration"] + 15)["StatusCode"]
            text = runner.logs().decode(errors="replace")
            if code:
                raise RuntimeError(text)
            result = json.loads(text)
            lines = target.logs(since=started, tail=4000).decode(errors="replace").splitlines()
            upstream = []
            for line in lines:
                try:
                    entry = json.loads(line)
                    if str(entry.get("request_id", "")).startswith(task_id + "-"):
                        upstream.append(entry)
                except ValueError:
                    continue
            result["upstream_logs"] = upstream
            result["upstream_logs_available"] = bool(upstream)
            result["runtime_config"] = actual_config.output.decode(errors="replace")
            result["runtime_nodes"] = actual_nodes
            return result
        finally:
            runner.remove(force=True)
