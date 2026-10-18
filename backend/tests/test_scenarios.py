import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from infra import db
from infra.api import app
from infra.scenario_catalog import CATALOG
from infra.worker import claim, execute, recover


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INFRA_DATA", str(tmp_path))
    with TestClient(app) as value:
        yield value


def create(client, template="nginx-weights", request_id="create-scenario-123"):
    response = client.post(f"/api/scenarios/{template}/labs", json={"request_id": request_id})
    assert response.status_code == 201, response.text
    return response.json()


def deploy_in_db(lab):
    with db.connect() as con:
        con.execute("UPDATE labs SET applied=draft,applied_revision=revision WHERE id=?", (lab["id"],))


def test_create_is_draft_only_idempotent_and_definitions_are_versioned(client, monkeypatch):
    assert len(client.get("/api/scenarios").json()) == 4
    lab = create(client)
    assert lab["scenario_id"] == "nginx-weights" and lab["applied"] is None
    assert len(lab["draft"]["nodes"]) == 4
    assert create(client)["id"] == lab["id"]
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM response_versions").fetchone()[0] == 0
    monkeypatch.setitem(CATALOG["nginx-weights"], "title", "future catalog title")
    state = client.get(f"/api/labs/{lab['id']}/scenario").json()
    assert state["template"]["title"] == "Nginx 权重分流"
    assert "recipe" not in state["template"]["steps"][0]
    assert (
        client.post("/api/scenarios/nginx-retry/labs", json={"request_id": "create-scenario-123"}).status_code
        == 409
    )
    before = {"base_revision": 1, "request_id": "run-before-apply"}
    assert client.post(f"/api/labs/{lab['id']}/scenario/steps/baseline/run", json=before).status_code == 409


def test_step_dedup_snapshot_and_real_measurement_dispatch(client):
    lab = create(client)
    deploy_in_db(lab)
    url = f"/api/labs/{lab['id']}"
    body = {"base_revision": 1, "request_id": "weighted-run-123", "count": 40, "concurrency": 1}
    first = client.post(url + "/scenario/steps/weighted/run", json=body)
    assert first.status_code == 202, first.text
    task = first.json()
    assert client.post(url + "/scenario/steps/weighted/run", json=body).json()["id"] == task["id"]
    assert client.post(url + "/scenario/steps/weighted/run", json={**body, "count": 41}).status_code == 409
    assert client.get(url).json()["revision"] == 2
    assert client.get(url).json()["applied_revision"] == 1
    runtime = Mock()
    runtime.observe.return_value = {"available": True, "nodes": [{"id": "app-a", "status": "running"}]}
    runtime.load.return_value = {
        "completed": 40,
        "requested": 40,
        "errors": 0,
        "distribution": {"app-a": 22, "app-b": 9, "app-c": 9},
        "runtime_config": "actual config",
    }
    claimed = claim()
    result = execute(claimed, runtime)
    runtime.apply.assert_called_once()
    runtime.load.assert_called_once()
    assert runtime.load.call_args.args[1]["count"] == 40
    db.finish(task["id"], "succeeded", result)
    run = client.get(url + "/scenario").json()["runs"][0]
    assert run["status"] == "succeeded" and run["parameters"]["count"] == 40
    assert run["snapshot"]["before"]["applied_revision"] == 1
    assert run["snapshot"]["after"]["applied_revision"] == 2
    assert run["result"]["load"]["distribution"] == runtime.load.return_value["distribution"]


def test_draft_and_response_conflicts_are_not_overwritten(client):
    lab = create(client, "nginx-timeout")
    deploy_in_db(lab)
    url = f"/api/labs/{lab['id']}"
    changed = lab["draft"]
    changed["nodes"][0]["config"] += "\n# user change\n"
    saved = client.put(url + "/draft", json={"base_revision": 1, "topology": changed}).json()
    body = {"base_revision": 2, "request_id": "draft-conflict-123"}
    assert client.post(url + "/scenario/steps/slow/run", json=body).status_code == 409
    assert client.get(url).json()["draft"] == saved["draft"]
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM response_versions").fetchone()[0] == 0
    other = create(client, "nginx-retry", "create-retry-123")
    deploy_in_db(other)
    path = f"/api/labs/{other['id']}/nodes/app-a/response"
    custom = {
        "mode": "custom",
        "format": "text",
        "status": 200,
        "body": "user draft",
        "delay_ms": 0,
        "headers": [],
    }
    assert client.put(path, json={"base_version": 0, "config": custom}).status_code == 200
    rejected = client.post(
        f"/api/labs/{other['id']}/scenario/steps/error/run",
        json={"base_revision": 1, "request_id": "response-conflict-123"},
    )
    assert rejected.status_code == 409
    assert client.get(path).json()["config"] == custom


def test_interruption_preserves_partial_evidence_and_no_automatic_replay(client):
    lab = create(client, "nginx-retry")
    deploy_in_db(lab)
    url = f"/api/labs/{lab['id']}"
    response = client.post(
        url + "/scenario/steps/baseline/run",
        json={"base_revision": 1, "request_id": "interrupt-step-123", "trace": True},
    )
    task_id = response.json()["id"]
    claim()
    partial = {"completed_actions": [{"action": "observed before restart", "time": db.now()}]}
    with db.connect() as con:
        con.execute("UPDATE scenario_runs SET result=? WHERE task_id=?", (json.dumps(partial), task_id))
    recover()
    run = client.get(url + "/scenario").json()["runs"][0]
    assert run["status"] == "interrupted"
    assert run["result"]["completed_actions"] == partial["completed_actions"]
    assert "未知" in run["result"]["error"]
    assert run["snapshot"]["after"] is None and claim() is None
    assert client.get(url + "/requests/" + task_id).json()["status"] == "interrupted"


def test_delete_erases_runs_and_creation_retry_does_not_recreate(client):
    lab = create(client, "redis-replication")
    deploy_in_db(lab)
    url = f"/api/labs/{lab['id']}"
    body = {"base_revision": 1, "request_id": "redis-step-123"}
    response = client.post(url + "/scenario/steps/replicate/run", json=body)
    assert response.status_code == 202
    assert (
        client.post(
            url + "/tasks",
            json={
                "kind": "scenario_step",
                "step_id": "replicate",
                "revision": 1,
                "request_id": "bypass-step-123",
            },
        ).status_code
        == 422
    )
    db.erase_lab(lab["id"])
    assert (
        client.post(
            "/api/scenarios/redis-replication/labs", json={"request_id": "create-scenario-123"}
        ).status_code
        == 410
    )
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM scenario_runs").fetchone()[0] == 0


def test_control_interruption_does_not_erase_confirmed_http_response(client):
    lab = create(client, "nginx-retry")
    deploy_in_db(lab)
    url = f"/api/labs/{lab['id']}"
    task = client.post(url + "/scenario/steps/baseline/run", json={"base_revision": 1, "request_id": "finished-http-123", "trace": True}).json()
    claim()
    confirmed = {"status_code": 200, "body": "already received", "trace_complete": True}
    db.request_status(task["id"], "completed", confirmed)
    recover()
    detail = client.get(url + "/requests/" + task["id"]).json()
    assert detail["status"] == "completed" and detail["result"] == confirmed
    assert client.get(url + "/scenario").json()["runs"][0]["status"] == "interrupted"
