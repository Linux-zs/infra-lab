"""Exercise every built-in scenario on real services; retain labs until cleanup.

Use an existing loopback SSH tunnel. Recovery restarts control services only when
--allow-control-restart is explicitly supplied and other labs have no active work.
"""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class Checks:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.output)
        if args.phase == "core":
            if self.output.exists():
                raise ValueError(
                    "Choose a new output file; old evidence is never overwritten"
                )
            self.evidence = {"token": uuid.uuid4().hex[:8], "labs": {}, "checks": []}
        else:
            self.evidence = json.loads(self.output.read_text(encoding="utf-8"))

    def record(self, name, value):
        self.evidence["checks"].append(
            {"name": name, "time": time.time(), "value": value}
        )
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(
            json.dumps(self.evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(name, json.dumps(value, ensure_ascii=False)[:800], flush=True)

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
            assert response.status == expected, (path, response.status, data)
            return data

    def remote(self, code):
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.args.ssh_server,
                "docker exec -i infra-lab-api-1 python -",
            ],
            input=code,
            capture_output=True,
            text=True,
            check=True,
            timeout=45,
        )
        return json.loads(result.stdout)

    def containers(self):
        return self.remote(
            "import docker,json\nprint(json.dumps({c.id:{'status':c.status,'lab':c.labels.get('infra.lab')} "
            "for c in docker.from_env().containers.list(all=True,ignore_removed=True) if c.labels.get('com.docker.compose.project')!='infra-lab'}))"
        )

    def scope(self, lab_id):
        assert lab_id in self.evidence["labs"]
        lab = self.api("/labs/" + lab_id)
        assert lab["name"] == self.evidence["labs"][lab_id]["name"]
        return lab

    def wait(self, read, condition, timeout=150):
        end = time.monotonic() + timeout
        value = None
        while time.monotonic() < end:
            value = read()
            if condition(value):
                return value
            time.sleep(0.25)
        raise AssertionError(f"Timed out; last value {value}")

    def wait_task(self, lab_id, task_id, expected="succeeded"):
        value = self.wait(
            lambda: next(
                t for t in self.api(f"/labs/{lab_id}/tasks") if t["id"] == task_id
            ),
            lambda t: t["status"] not in ("queued", "running"),
        )
        assert value["status"] == expected, {
            k: value[k] for k in ("id", "status", "result", "logs")
        }
        return value

    def task(self, lab_id, kind):
        lab = self.scope(lab_id)
        created = self.api(
            f"/labs/{lab_id}/tasks",
            "POST",
            {"kind": kind, "revision": lab["revision"], "request_id": uuid.uuid4().hex},
            202,
        )
        return self.wait_task(lab_id, created["id"])

    def submit_step(self, lab_id, step):
        lab = self.scope(lab_id)
        body = {
            "base_revision": lab["revision"],
            "request_id": uuid.uuid4().hex,
            "count": 120,
            "concurrency": 1,
            "duration": 30,
            "trace": True,
            "value": "场景真实复制",
        }
        path = f"/labs/{lab_id}/scenario/steps/{step}/run"
        created = self.api(path, "POST", body, 202)
        assert self.api(path, "POST", body, 202)["id"] == created["id"]
        return created

    def isolation(self):
        current = {v["id"]: v for v in self.api("/labs")}
        for lab_id, before in self.evidence["baseline_labs"].items():
            assert (
                lab_id in current and {k: current[lab_id][k] for k in before} == before
            ), lab_id
        containers = self.containers()
        for container_id, before in self.evidence["baseline_containers"].items():
            assert containers.get(container_id) == before, container_id
        self.record("original_labs_unchanged", True)

    def core(self):
        health = self.api("/health")
        assert not health["worker_stale"] and health["worker"]["docker"]
        self.evidence["baseline_labs"] = {
            v["id"]: {
                k: v[k]
                for k in ("name", "draft", "revision", "applied", "applied_revision")
            }
            for v in self.api("/labs")
        }
        self.evidence["baseline_containers"] = self.containers()
        for template in self.api("/scenarios"):
            body = {
                "request_id": uuid.uuid4().hex,
                "name": f"场景验收 {self.evidence['token']} {template['title']}",
            }
            lab = self.api(f"/scenarios/{template['id']}/labs", "POST", body, 201)
            lab_id = lab["id"]
            self.evidence["labs"][lab_id] = {
                "template_id": template["id"],
                "name": body["name"],
                "creation": body,
            }
            assert lab["applied"] is None and lab["revision"] == 1
            assert (
                self.api(f"/scenarios/{template['id']}/labs", "POST", body, 201)["id"]
                == lab_id
            )
            assert not any(v["lab"] == lab_id for v in self.containers().values())
            self.record(
                "created_draft_only", {"lab_id": lab_id, "template": template["id"]}
            )
            self.task(lab_id, "apply")
            if template["id"] == "redis-replication":
                self.wait(
                    lambda selected=lab_id: self.scope(selected)["observation"],
                    lambda v: (
                        v
                        and len(v["nodes"]) == 3
                        and all(
                            n.get("redis", {})
                            .get("fields", {})
                            .get("master_link_status")
                            == "up"
                            for n in v["nodes"]
                            if n["id"] != "redis-primary"
                        )
                    ),
                )
            runs = []
            for step in template["steps"]:
                task = self.submit_step(lab_id, step["id"])
                self.wait_task(lab_id, task["id"])
                run = next(
                    r
                    for r in self.api(f"/labs/{lab_id}/scenario")["runs"]
                    if r["task_id"] == task["id"]
                )
                assert (
                    run["status"] == "succeeded"
                    and run["snapshot"]["before"]
                    and run["snapshot"]["after"]
                )
                runs.append(run)
                self.record(template["id"] + "/" + step["id"], run)
                measurement = run["result"]
                if template["id"] == "nginx-weights":
                    assert (
                        measurement["load"]["completed"] == 120
                        and measurement["load"]["errors"] == 0
                    )
                    assert sum(measurement["load"]["distribution"].values()) == 120, measurement["load"]
                if template["id"] == "nginx-retry":
                    request = measurement["request"]
                    assert request["status_code"] == 200 and request["trace_complete"]
                    assert request["node"] == (
                        "app-b" if step["id"] in ("error", "stop") else "app-a"
                    )
                    if step["id"] == "error":
                        attempts = next(
                            e
                            for e in request["events"]
                            if e["kind"] == "nginx.completed"
                        )["data"]["attempts"]
                        assert [(a["node_id"], a["status"]) for a in attempts] == [
                            ("app-a", "503"),
                            ("app-b", "200"),
                        ]
                if template["id"] == "nginx-timeout":
                    request = measurement["request"]
                    if step["id"] == "slow":
                        assert request["status_code"] == 504
                    elif step["id"] == "client-timeout":
                        assert (
                            request["error_kind"] == "timeout"
                            and request["bytes_captured"] < 16384
                        )
                        kinds = [e["kind"] for e in request["events"]]
                        assert (
                            "app.response_written" in kinds
                            and "client.timeout" in kinds
                        )
                        written = next(
                            e["time"]
                            for e in request["events"]
                            if e["kind"] == "app.response_written"
                        )
                        timed_out = next(
                            e["time"]
                            for e in request["events"]
                            if e["kind"] == "client.timeout"
                        )
                        assert written < timed_out
                    else:
                        assert request["status_code"] == 200 and not request["error"]
                if template["id"] == "redis-replication":
                    commands = measurement["commands"]
                    if step["id"] == "readonly":
                        assert (
                            commands[0]["expected_error"]
                            and "READONLY" in commands[0]["error"]
                        )
                    else:
                        assert all(
                            c["value"] == "场景真实复制"
                            for c in commands
                            if c["operation"] == "GET"
                        ), commands
            if template["id"] == "nginx-weights":
                assert (
                    runs[1]["result"]["load"]["distribution"]["app-a"]
                    > runs[0]["result"]["load"]["distribution"]["app-a"]
                )
        self.evidence["core_passed"] = True
        self.isolation()

    def restart(self, service):
        assert self.args.allow_control_restart and service in ("worker", "api")
        for lab in self.api("/labs"):
            active = [
                t
                for t in self.api(f"/labs/{lab['id']}/tasks")
                if t["status"] in ("queued", "running")
            ]
            assert lab["id"] in self.evidence["labs"] or not active, (
                "Another lab is busy"
            )
        subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.args.ssh_server,
                f"docker restart -t 0 infra-lab-{service}-1",
            ],
            check=True,
            capture_output=True,
            timeout=45,
        )
        end = time.monotonic() + 45
        while time.monotonic() < end:
            try:
                if not self.api("/health")["worker_stale"]:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        raise TimeoutError(service)

    def recovery(self):
        assert self.evidence.get("core_passed")
        lab_id = next(
            k
            for k, v in self.evidence["labs"].items()
            if v["template_id"] == "nginx-timeout"
        )
        task = self.submit_step(lab_id, "slow")
        self.wait(
            lambda: self.api(f"/labs/{lab_id}/requests/{task['id']}"),
            lambda r: any(e["kind"] == "app.processing" for e in r["events"]),
        )
        self.restart("worker")
        self.wait_task(lab_id, task["id"], "interrupted")
        state = self.api(f"/labs/{lab_id}/scenario")
        interrupted = next(r for r in state["runs"] if r["task_id"] == task["id"])
        assert (
            interrupted["result"]["completed_actions"]
            and interrupted["snapshot"]["after"] is None
        )
        assert "未知" in interrupted["result"]["error"]
        self.restart("api")
        assert self.api(f"/labs/{lab_id}/scenario")["runs"] == state["runs"]
        self.record(
            "interruption_keeps_partial_evidence_and_history_recovers", interrupted
        )
        restored = self.submit_step(lab_id, "recover")
        self.wait_task(lab_id, restored["id"])
        self.isolation()

    def cleanup(self):
        for lab_id, item in self.evidence["labs"].items():
            if not any(lab["id"] == lab_id for lab in self.api("/labs")):
                continue
            lab = self.scope(lab_id)
            self.api(
                f"/labs/{lab_id}/tasks",
                "POST",
                {
                    "kind": "delete_lab",
                    "revision": lab["revision"],
                    "request_id": uuid.uuid4().hex,
                },
                202,
            )
            self.wait(
                lambda: self.api("/labs"),
                lambda labs, deleted=lab_id: all(v["id"] != deleted for v in labs),
            )
            if item.get("creation"):
                self.api(
                    f"/scenarios/{item['template_id']}/labs",
                    "POST",
                    item["creation"],
                    410,
                )
        remaining = self.remote(
            "import docker,json\nc=docker.from_env()\nlabs="
            + repr(list(self.evidence["labs"]))
            + "\nprint(json.dumps({lab:{'containers':len(c.containers.list(all=True,filters={'label':['infra.owner=infra-lab-v1','infra.lab='+lab]},ignore_removed=True)),'networks':len(c.networks.list(filters={'label':['infra.owner=infra-lab-v1','infra.lab='+lab]}))} for lab in labs}))"
        )
        assert all(v == {"containers": 0, "networks": 0} for v in remaining.values())
        self.isolation()
        self.record("test_resources_removed", remaining)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8088")
    parser.add_argument("--ssh-server", required=True)
    parser.add_argument("--output", default="artifacts/scenario-acceptance.json")
    parser.add_argument(
        "--phase", choices=("core", "recovery", "cleanup"), default="core"
    )
    parser.add_argument("--allow-control-restart", action="store_true")
    args = parser.parse_args()
    if args.phase == "recovery" and not args.allow_control_restart:
        parser.error("recovery requires --allow-control-restart")
    checks = Checks(args)
    try:
        getattr(checks, args.phase)()
    except Exception as exc:
        checks.record(
            args.phase + "_failed",
            {"type": type(exc).__name__, "message": str(exc)[:4000]},
        )
        raise
    checks.record(args.phase + "_passed", True)


if __name__ == "__main__":
    main()
