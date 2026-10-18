"""Real release regression in fresh labelled labs, using the SSH tunnel.

Core checks retain their labs for browser inspection. Cleanup is an explicit phase.
Recovery additionally requires --ssh-server and --allow-control-restart.
Evidence records generated test data, request IDs and actual resource states.
"""

import argparse
import concurrent.futures
import copy
import http.client
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def eventually(read, predicate, timeout=90):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.25)
    raise AssertionError(f"Timed out; last observation: {value!r}")


class Suite:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.output)
        if args.phase == "core":
            if self.output.exists():
                raise ValueError("Evidence already exists; choose a new --output path")
            self.evidence = {"run": uuid.uuid4().hex[:10], "checks": [], "labs": []}
        else:
            self.evidence = json.loads(self.output.read_text(encoding="utf-8"))
        self.root = "/labs/" + self.evidence.get("lab_id", "")

    def record(self, name, value):
        self.evidence["checks"].append({"name": name, "time": time.time(), "value": value})
        self.flush()
        print(name + ": " + json.dumps(value, ensure_ascii=False), flush=True)

    def flush(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    def api(self, path, method="GET", body=None, expected=200):
        request = urllib.request.Request(
            self.args.url.rstrip("/") + "/api" + path,
            method=method,
            headers={"Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            response = urllib.request.urlopen(request, timeout=40)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = json.load(response)
            if response.status != expected:
                raise AssertionError(f"{method} {path}: {response.status}: {data}")
            return data

    def remote(self, code):
        if not self.args.ssh_server:
            raise ValueError("This check requires --ssh-server")
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", self.args.ssh_server,
             "docker exec -i infra-lab-api-1 python -"],
            input=code, text=True, capture_output=True, check=True, timeout=45,
        )
        return json.loads(result.stdout)

    def containers(self):
        return self.remote(
            "import docker,json\n"
            "print(json.dumps({c.id:{'status':c.status,'lab':c.labels.get('infra.lab'),"
            "'owner':c.labels.get('infra.owner'),'node':c.labels.get('infra.node'),"
            "'role':c.labels.get('infra.role')} for c in docker.from_env().containers.list(all=True) "
            "if c.labels.get('com.docker.compose.project')!='infra-lab'}))"
        )

    def scoped(self, lab_id=None):
        lab_id = lab_id or self.evidence["lab_id"]
        lab = self.api("/labs/" + lab_id)
        assert lab_id in self.evidence["labs"] and lab["name"] == self.evidence["lab_name"]
        return lab

    def save(self, topology):
        lab = self.scoped()
        return self.api(self.root + "/draft", "PUT", {"base_revision": lab["revision"], "topology": topology})

    def submit(self, kind, lab_id=None, **extra):
        lab = self.scoped(lab_id)
        path = "/labs/" + lab["id"] + "/tasks"
        body = {"kind": kind, "revision": lab["revision"], "request_id": uuid.uuid4().hex, **extra}
        first = self.api(path, "POST", body, 202)
        if kind != "delete_lab":
            duplicate = self.api(path, "POST", body, 202)
            assert first["id"] == duplicate["id"], "Request de-duplication failed"
        return first

    def wait(self, task, expected="succeeded"):
        path = "/labs/" + task["lab_id"] + "/tasks"
        value = eventually(
            lambda: next(t for t in self.api(path) if t["id"] == task["id"]),
            lambda t: t["status"] not in ("running", "queued"), timeout=150,
        )
        assert value["status"] == expected, {k: value[k] for k in ("id", "kind", "status", "result", "logs")}
        return value

    def task(self, kind, expected="succeeded", **extra):
        return self.wait(self.submit(kind, **extra), expected)

    def observed(self, predicate):
        return eventually(lambda: {n["id"]: n for n in (self.scoped().get("observation") or {}).get("nodes", [])}, predicate)

    def configure(self, node="app-a", apply=True, **patch):
        path = self.root + "/nodes/" + node + "/response"
        saved = self.api(path)
        config = {"mode": "custom", "status": 200, "format": "text", "body": "release-response", "delay_ms": 0, "headers": [], **patch}
        saved = self.api(path, "PUT", {"base_version": saved["version"], "config": config})
        if apply:
            self.task("apply_response", node_id=node, response_version=saved["version"])
        return saved

    def visit(self, path="/", method="GET", body=None, cookie=None):
        portal = urllib.parse.urlsplit(self.args.portal_url)
        connection = http.client.HTTPConnection(portal.hostname, portal.port or 80, timeout=35)
        headers = {"Host": self.evidence["entry"]["hostname"] + ":" + str(portal.port or 80),
                   "Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return {"status": response.status, "headers": response.getheaders(), "body": response.read().decode("utf-8", errors="replace")}
        finally:
            connection.close()

    def detail(self, request_id):
        return eventually(lambda: self.api(self.root + "/requests/" + request_id), lambda v: v["status"] not in ("queued", "running"))

    def request(self, path="/page", trace=True):
        task = self.task("client_request", node_id="gateway", path=path, trace=trace)
        return self.detail(task["id"])

    def sse_first(self, cursor):
        request = urllib.request.Request(
            self.args.url.rstrip("/") + "/api" + self.root + "/requests/events",
            headers={"Last-Event-ID": str(cursor)},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            while True:
                line = response.readline().decode().strip()
                if line.startswith("data: "):
                    value = json.loads(line[6:])
                    if isinstance(value, dict) and "seq" in value:
                        return value

    def validate_response_inputs(self):
        path = self.root + "/nodes/app-a/response"
        saved = self.api(path)
        baseline = {"mode": "custom", "status": 200, "format": "text", "body": "valid", "delay_ms": 0, "headers": []}
        for patch in ({"format": "json", "body": "invalid"}, {"body": "x" * 65537}, {"status": 204, "body": "invalid"}, {"headers": [{"name": "X-Invalid", "value": "one\r\ntwo"}]}):
            self.api(path, "PUT", {"base_version": saved["version"], "config": {**baseline, **patch}}, 422)
        self.api(path, "PUT", {"base_version": saved["version"] - 1, "config": saved["config"]}, 409)
        assert self.api(path)["version"] == saved["version"]

    def core(self):
        health = self.api("/health")
        assert health["worker"]["docker"] and not health["worker_stale"], health
        baseline = self.api("/labs")
        self.evidence["baseline_labs"] = {v["id"]: {k: v[k] for k in ("name", "revision", "draft", "applied", "applied_revision")} for v in baseline}
        if self.args.ssh_server:
            self.evidence["baseline_containers"] = self.containers()
        self.evidence["lab_name"] = "发布回归 " + self.evidence["run"]
        for _ in range(2):
            lab = self.api("/labs", "POST", {"name": self.evidence["lab_name"]}, 201)
            self.evidence["labs"].append(lab["id"])
            self.flush()
        self.evidence["lab_id"], self.evidence["guard_id"] = self.evidence["labs"]
        self.root = "/labs/" + self.evidence["lab_id"]
        guard = self.scoped(self.evidence["guard_id"])
        guard_node = {"id": "guard-app", "kind": "app", "position": {"x": 80, "y": 80}, "memory_mb": 64, "cpu": .25}
        self.api("/labs/" + guard["id"] + "/draft", "PUT", {"base_revision": guard["revision"], "topology": {"nodes": [guard_node], "edges": []}})
        self.wait(self.submit("apply", lab_id=guard["id"]))
        names = [("gateway", "nginx", 0, 100), ("app-a", "app", 360, 0), ("app-b", "app", 360, 210),
                 ("redis-primary", "redis", 0, 420), ("redis-replica-1", "redis", 360, 420), ("redis-replica-2", "redis", 700, 420)]
        nodes = [{"id": name, "kind": kind, "position": {"x": x, "y": y}, "memory_mb": 64, "cpu": .25} for name, kind, x, y in names]
        edges = [{"source": "gateway", "target": "app-a", "weight": 1}, {"source": "redis-primary", "target": "redis-replica-1", "weight": 1}, {"source": "redis-replica-1", "target": "redis-replica-2", "weight": 1}]
        draft = self.save({"nodes": nodes, "edges": edges})["draft"]
        draft["nodes"][0].update(mode="manual", config=draft["nodes"][0]["config"].replace("proxy_read_timeout 2s", "proxy_read_timeout 10s"))
        self.save(draft)
        self.task("apply")
        before = self.observed(lambda v: len(v) == 6 and all(n["health"] == "healthy" for n in v.values()))
        ids = {k: v["container_id"] for k, v in before.items()}
        self.task("apply")
        assert {k: v["container_id"] for k, v in self.observed(lambda v: len(v) == 6).items()} == ids
        self.record("real_nodes_and_idempotent_apply", ids)
        self.evidence["entry"] = self.api(self.root + "/entries", "POST", {"node_id": "gateway", "trace": True})
        self.flush()
        for path in ("/", "/details", "/search?q=release", "/assets/site.css", "/assets/site.js", "/api/instance"):
            response = self.visit(path)
            assert response["status"] == 200, response["status"]
            request_id = dict(response["headers"])["x-request-id"]
            detail = self.detail(request_id)
            assert detail["source"] == "browser" and detail["result"]["trace_complete"], detail
            assert detail["result"]["body"] is None
        form = self.visit("/submit", "POST", "message=private-release-form")
        assert "private-release-form" in form["body"]
        detail = self.detail(dict(form["headers"])["x-request-id"])
        assert "private-release-form" not in json.dumps(detail)
        assert len([e for e in detail["events"] if e["kind"] == "app.received"]) == 1
        assert self.visit("/redirect")["status"] == 302
        cookie = json.loads(self.visit("/api/instance", cookie="infra_demo=visited")["body"])
        assert cookie["cookie_present"]
        single = self.request()
        assert single["id"] in single["result"]["body"] and "app-a" in single["result"]["body"]
        assert single["result"]["trace_complete"]
        cursor = single["events"][-1]["seq"]
        self.visit("/details?resume=1")
        resumed = self.sse_first(cursor)
        assert resumed["seq"] > cursor
        self.record("site_forms_assets_cookies_preview_and_sse_resume", {"request_id": single["id"], "cursor": cursor, "resumed_seq": resumed["seq"]})

        saved = self.configure(format="json", body='{"message":"真实响应"}', headers=[{"name": "X-Release", "value": "yes"}, {"name": "Set-Cookie", "value": "a=1; Path=/"}, {"name": "Set-Cookie", "value": "b=2; Path=/"}])
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            response = self.visit("/custom?x=1", method)
            headers = {k.lower(): v for k, v in response["headers"]}
            assert response["status"] == 200 and headers["x-infra-response-version"] == str(saved["version"])
            assert headers["x-infra-node"] == "app-a" and headers["x-release"] == "yes"
            assert response["body"] == ("" if method == "HEAD" else saved["config"]["body"])
            assert sum(k.lower() == "set-cookie" for k, _ in response["headers"]) == 2
        for fmt, body, status in (("text", "真实文本", 404), ("html", "<h1>真实页面</h1>", 503), ("text", "", 204), ("text", "", 205), ("text", "", 304)):
            self.configure(format=fmt, body=body, status=status)
            response = self.visit()
            assert response["status"] == status and response["body"] == body
        self.validate_response_inputs()
        self.record("response_formats_methods_headers_validation_and_conflicts", True)

        slow_version = self.configure(body="old-snapshot", delay_ms=4500)["version"]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(self.visit, "/slow-snapshot")
            def processing():
                matches = [r for r in self.api(self.root + "/requests") if r["path"] == "/slow-snapshot"]
                return self.api(self.root + "/requests/" + matches[0]["id"]) if matches else None
            active = eventually(processing, lambda d: d and d["status"] == "running" and any(e["kind"] == "app.processing" for e in d["events"]))
            assert not pending.done()
            self.configure(body="new-snapshot")
            response = pending.result(timeout=20)
        assert response["body"] == "old-snapshot" and self.visit()["body"] == "new-snapshot"
        assert int(dict(response["headers"])["x-infra-response-version"]) == slow_version
        topology = self.scoped()["draft"]
        topology["nodes"][1]["delay_ms"] = 777
        self.save(topology)
        self.configure(body="independent-response")
        self.task("reload", node_id="gateway")
        lab = self.scoped()
        assert lab["draft"]["nodes"][1]["delay_ms"] == 777 and lab["applied"]["nodes"][1]["delay_ms"] == 0
        assert {k: v["container_id"] for k, v in self.observed(lambda v: len(v) == 6).items()} == ids
        self.record("inflight_snapshot_live_processing_independent_apply_and_reload", {"request_id": active["id"], "version": slow_version})

        good = copy.deepcopy(lab["draft"])
        good["nodes"][1]["delay_ms"] = 0
        bad = copy.deepcopy(good)
        bad["nodes"][0].update(mode="manual", config="invalid_directive;\n")
        self.save(bad)
        self.task("apply", expected="failed")
        assert self.visit()["body"] == "independent-response"
        assert self.scoped()["applied_revision"] == lab["applied_revision"]
        self.save(good)
        self.task("apply")
        self.configure(node="app-b", body="fallback-response")
        self.configure(status=503, body="primary-error")
        topology = self.scoped()["draft"]
        gateway = topology["nodes"][0]
        gateway.update(mode="manual", config=gateway["config"].replace("upstream backends {", "upstream backends {\n    server app-b:8080 backup;"))
        self.save(topology)
        self.task("apply")
        response = self.visit("/retry-503")
        assert response["status"] == 200 and response["body"] == "fallback-response"
        detail = self.detail(dict(response["headers"])["x-request-id"])
        attempts = next(e for e in detail["events"] if e["kind"] == "nginx.completed")["data"]["attempts"]
        assert [(a["node_id"], a["status"]) for a in attempts] == [("app-a", "503"), ("app-b", "200")], attempts
        self.task("stop", node_id="app-a")
        assert self.visit()["body"] == "fallback-response"
        self.task("start", node_id="app-a")
        self.record("invalid_nginx_preserves_running_and_real_retry", attempts)

        def redis(node, op, **extra):
            return self.task("redis_command", node_id=node, redis_op=op, **extra)["result"]["value"]
        self.observed(lambda v: all(v[n]["redis"]["fields"].get("master_link_status") == "up" for n in ("redis-replica-1", "redis-replica-2")))
        assert redis("redis-primary", "SET", key="release", value="真实复制") == "OK"
        for name in ("redis-replica-1", "redis-replica-2"):
            eventually(lambda node=name: redis(node, "GET", key="release"), lambda v: v == "真实复制")
        failed = self.task("redis_command", expected="failed", node_id="redis-replica-1", redis_op="SET", key="release", value="no")
        assert "READONLY" in failed["result"]["error"]
        self.task("stop", node_id="redis-primary")
        self.observed(lambda v: v["redis-replica-1"]["redis"]["fields"].get("master_link_status") == "down")
        assert redis("redis-replica-2", "GET", key="release") == "真实复制"
        self.task("start", node_id="redis-primary")
        self.observed(lambda v: v["redis-replica-1"]["redis"]["fields"].get("master_link_status") == "up")
        self.record("redis_cascade_replication_readonly_and_primary_recovery", True)

        self.task("stop_lab")
        stopped = self.observed(lambda v: all(n["status"] == "exited" for n in v.values()))
        assert {k: v["container_id"] for k, v in stopped.items()} == ids
        assert self.visit()["status"] == 503
        assert all(n["status"] == "skipped" for n in self.task("stop_lab")["result"]["nodes"])
        guard = eventually(lambda: self.scoped(self.evidence["guard_id"]), lambda v: v.get("observation") and v["observation"]["nodes"])
        assert guard["observation"]["nodes"][0]["status"] == "running"
        self.task("start_lab")
        self.observed(lambda v: all(n["status"] == "running" and n["health"] == "healthy" for n in v.values()))
        assert all(n["status"] == "skipped" for n in self.task("start_lab")["result"]["nodes"])
        assert redis("redis-primary", "GET", key="release") == "真实复制"
        self.record("whole_lab_lifecycle_preserves_ids_data_and_other_lab", True)
        self.configure(mode="website")
        self.configure(node="app-b", mode="website")
        self.task("reload", node_id="gateway")
        entry_path = self.root + "/entries/" + self.evidence["entry"]["id"]
        self.api(entry_path, "PATCH", {"trace": False})
        untraced = self.visit("/trace-disabled")
        assert self.detail(dict(untraced["headers"])["x-request-id"])["events"] == []
        assert self.request("/api/instance", False)["events"] == []
        self.api(entry_path, "DELETE")
        assert self.visit()["status"] == 410
        self.evidence["entry"] = self.api(self.root + "/entries", "POST", {"node_id": "gateway", "trace": True})
        self.evidence["core_passed"] = True
        self.record("trace_off_revocation_and_ready_for_browser", self.evidence["entry"]["url"])
        self.verify_isolation()

    def limits(self):
        assert self.evidence.get("core_passed") and self.args.ssh_server
        self.validate_response_inputs()
        self.record("independent_json_header_body_size_and_status_validation", True)
        topology = copy.deepcopy(self.scoped()["draft"])
        lab_id = self.evidence["lab_id"]
        self.remote(
            "import docker,io,tarfile,json\n"
            "c=docker.from_env().containers.list(filters={'label':['infra.owner=infra-lab-v1',"
            + repr("infra.lab=" + lab_id) + ",'infra.node=gateway','infra.role=node']})\n"
            "assert len(c)==1\nb=io.BytesIO()\n"
            "with tarfile.open(fileobj=b,mode='w') as t:\n"
            " f=tarfile.TarInfo('infra-release-large.txt'); f.size=2*1048576; f.mode=0o644; t.addfile(f,io.BytesIO(b'x'*f.size))\n"
            "assert c[0].put_archive('/tmp',b.getvalue())\nprint(json.dumps(True))"
        )
        try:
            changed = copy.deepcopy(topology)
            changed["nodes"][0]["config"] = changed["nodes"][0]["config"].replace(
                "    location / {",
                "    location = /release-large { alias /tmp/infra-release-large.txt; }\n"
                "    location = /release-timeout { alias /tmp/infra-release-large.txt; limit_rate 1; }\n"
                "    location = /release-503 { return 503; }\n    location / {",
            )
            self.save(changed)
            self.task("apply")
            single = self.request("/release-large", False)["result"]
            assert single["truncated"] and single["bytes_captured"] == 1048576
            browser = self.visit("/release-large")
            assert browser["status"] == 200 and len(browser["body"]) == 2 * 1048576
            timeout = self.request("/release-timeout", False)["result"]
            assert timeout["error_kind"] == "timeout", timeout
            failure = self.request("/release-503")
            assert failure["result"]["status_code"] == 503 and "nginx" in failure["result"]["body"].lower()
            assert not failure["result"]["trace_complete"]
            assert self.visit("/submit", "POST", "x" * 1048577)["status"] == 413
            self.record("real_nginx_error_timeout_preview_limit_and_untruncated_website", {"captured": single["bytes_captured"], "website_bytes": len(browser["body"]), "timeout_kind": timeout["error_kind"]})
            missing = copy.deepcopy(topology)
            missing["nodes"][0]["config"] = missing["nodes"][0]["config"].replace("access_log /dev/stdout lab;", "access_log off;")
            self.save(missing)
            self.task("apply")
            detail = self.request("/api/instance?logs=off")
            assert not detail["result"]["trace_complete"]
            assert any(e["kind"] == "app.received" for e in detail["events"])
            assert not any(e["kind"] == "nginx.completed" for e in detail["events"])
            self.record("missing_logs_preserve_partial_evidence", detail["id"])
        finally:
            self.save(topology)
            self.task("apply")

    def verify_isolation(self):
        current = {lab["id"]: lab for lab in self.api("/labs")}
        for lab_id, before in self.evidence["baseline_labs"].items():
            assert lab_id in current, f"Baseline lab disappeared during acceptance: {lab_id}"
            assert {k: current[lab_id][k] for k in before} == before, lab_id
        if self.args.ssh_server:
            current_containers = self.containers()
            for container_id, before in self.evidence["baseline_containers"].items():
                assert current_containers.get(container_id) == before, container_id
        self.record("existing_labs_and_containers_unchanged", True)

    def restart(self, service):
        assert self.args.allow_control_restart and service in ("api", "worker", "portal")
        for lab in self.api("/labs"):
            active = [t for t in self.api("/labs/" + lab["id"] + "/tasks") if t["status"] in ("queued", "running")]
            assert lab["id"] == self.evidence["lab_id"] or not active, "Another lab is busy; restart refused"
            if service == "portal" and lab["id"] != self.evidence["lab_id"]:
                requests = self.api("/labs/" + lab["id"] + "/requests")
                assert not any(r["source"] == "browser" and r["status"] == "running" for r in requests), "Another website has an active request; restart refused"
        subprocess.run(["ssh", "-o", "BatchMode=yes", self.args.ssh_server,
                        f"docker restart -t 0 infra-lab-{service}-1"], check=True, capture_output=True, timeout=45)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                if not self.api("/health")["worker_stale"]:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.5)
        raise TimeoutError("Control service did not recover")

    def recovery(self):
        assert self.evidence.get("core_passed") and self.args.ssh_server and self.args.allow_control_restart
        self.scoped()
        self.configure(delay_ms=9000, body="server-still-processing")
        topology = self.scoped()["draft"]
        topology["nodes"][0]["config"] = topology["nodes"][0]["config"].replace("proxy_read_timeout 10s", "proxy_read_timeout 20s").replace("proxy_read_timeout 2s", "proxy_read_timeout 20s")
        self.save(topology)
        self.task("apply")
        interrupted = self.submit("client_request", node_id="gateway", path="/worker-interruption", trace=True)
        request_path = self.root + "/requests/" + interrupted["id"]
        eventually(lambda: self.api(request_path), lambda d: any(e["kind"] == "app.processing" for e in d["events"]))
        self.restart("worker")
        self.wait(interrupted, "interrupted")
        state = self.detail(interrupted["id"])
        assert state["status"] == "interrupted" and "未知" in state["result"]["error"]
        self.record("worker_interruption_not_replayed", {"request_id": state["id"], "status": state["status"]})
        # Configuration changes do not cancel the previous server request.
        self.configure(mode="website")
        before = self.scoped()
        history = {r["id"] for r in self.api(self.root + "/requests")}
        self.restart("api")
        after = self.scoped()
        assert after["draft"] == before["draft"] and after["applied"] == before["applied"]
        assert history <= {r["id"] for r in self.api(self.root + "/requests")}
        assert self.api(request_path)["status"] == "interrupted"
        completed = self.request("/page?after=restart")
        assert completed["result"]["trace_complete"]
        cursor = completed["events"][-1]["seq"]
        self.visit("/details?sse=reconnect")
        assert self.sse_first(cursor)["seq"] > cursor
        self.record("api_restart_history_and_sse_reconnection", True)
        self.configure(delay_ms=7000, body="collector-interruption")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(self.visit, "/collector-interruption")
            def browser_request():
                return next((r for r in self.api(self.root + "/requests") if r["path"] == "/collector-interruption"), None)
            request = eventually(browser_request, lambda r: r is not None)
            eventually(lambda: self.api(self.root + "/requests/" + request["id"]), lambda d: any(e["kind"] == "app.processing" for e in d["events"]))
            self.restart("portal")
            try:
                pending.result(timeout=35)
            except (OSError, http.client.HTTPException):
                pass
        detail = self.detail(request["id"])
        assert detail["status"] == "interrupted" and not detail["result"]["trace_complete"]
        self.configure(mode="website")
        self.record("collector_restart_marks_incomplete_and_restores_entry", {"request_id": request["id"], "status": detail["status"]})
        assert self.visit()["status"] == 200
        self.verify_isolation()

    def cleanup(self):
        for lab_id in self.evidence["labs"]:
            if not any(lab["id"] == lab_id for lab in self.api("/labs")):
                continue
            self.submit("delete_lab", lab_id=lab_id)
            eventually(lambda: self.api("/labs"), lambda labs, removed=lab_id: all(v["id"] != removed for v in labs))
        if self.args.ssh_server:
            resources = self.remote("import docker,json\nc=docker.from_env()\n"
                "labs=" + repr(self.evidence["labs"]) + "\n"
                "print(json.dumps({lab:{'containers':len(c.containers.list(all=True,filters={'label':['infra.owner=infra-lab-v1','infra.lab='+lab]})),"
                "'networks':len(c.networks.list(filters={'label':['infra.owner=infra-lab-v1','infra.lab='+lab]}))} for lab in labs}))")
            assert all(v == {"containers": 0, "networks": 0} for v in resources.values()), resources
        self.verify_isolation()
        self.record("cleanup_only_test_labs", self.evidence["labs"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8088")
    parser.add_argument("--portal-url", default="http://127.0.0.1:8089")
    parser.add_argument("--output", default="artifacts/release-acceptance.json")
    parser.add_argument("--phase", choices=("core", "limits", "recovery", "cleanup"), default="core")
    parser.add_argument("--ssh-server", help="Existing SSH alias or user@host; no embedded password")
    parser.add_argument("--allow-control-restart", action="store_true")
    args = parser.parse_args()
    if args.phase == "recovery" and not (args.ssh_server and args.allow_control_restart):
        parser.error("recovery requires --ssh-server and --allow-control-restart")
    suite = Suite(args)
    try:
        getattr(suite, args.phase)()
    except Exception as exc:
        suite.record(args.phase + "_failed", {"type": type(exc).__name__, "message": str(exc)[:3000]})
        raise
    suite.record(args.phase + "_passed", True)


if __name__ == "__main__":
    main()
