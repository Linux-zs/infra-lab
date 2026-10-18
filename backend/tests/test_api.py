import pytest
from fastapi.testclient import TestClient

from infra import db
from infra.api import app
from infra.models import Topology, normalize
from infra.worker import claim, recover


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INFRA_DATA", str(tmp_path))
    with TestClient(app) as value:
        yield value


def graph():
    return {
        "nodes": [
            {"id": "gateway", "kind": "nginx", "position": {"x": 0, "y": 0}},
            {"id": "app-1", "kind": "app", "position": {"x": 300, "y": 0}},
        ],
        "edges": [{"source": "gateway", "target": "app-1", "weight": 3}],
    }


def test_revision_conflict_and_config_history(client):
    lab = client.post("/api/labs", json={"name": "test"}).json()
    url = f"/api/labs/{lab['id']}/draft"
    saved = client.put(url, json={"base_revision": 0, "topology": graph()})
    assert saved.status_code == 200
    assert "weight=3" in saved.json()["draft"]["nodes"][0]["config"]
    assert client.put(url, json={"base_revision": 0, "topology": graph()}).status_code == 409
    history = client.get(f"/api/labs/{lab['id']}/revisions").json()
    assert [r["revision"] for r in history] == [1, 0]


def test_task_idempotency_snapshot_and_recovery(client):
    lab = client.post("/api/labs", json={"name": "test"}).json()
    url = f"/api/labs/{lab['id']}"
    client.put(url + "/draft", json={"base_revision": 0, "topology": graph()})
    action = {"kind": "apply", "revision": 1, "request_id": "request-12345"}
    first = client.post(url + "/tasks", json=action).json()
    second = client.post(url + "/tasks", json=action).json()
    assert first["id"] == second["id"]
    assert client.post(url + "/tasks", json={**action, "kind": "cleanup"}).status_code == 409
    client.put(url + "/draft", json={"base_revision": 1, "topology": {"nodes": [], "edges": []}})
    claimed = claim()
    assert claimed["payload"]["topology"]["nodes"][0]["id"] == "gateway"
    assert claim() is None
    recover()
    task = client.get(url + "/tasks").json()[0]
    assert task["status"] == "interrupted"
    assert task["logs"]


def test_manual_config_is_preserved():
    topology = graph()
    topology["nodes"][0].update(mode="manual", config="intentionally invalid nginx config;")
    normalized = normalize(Topology.model_validate(topology))
    assert normalized["nodes"][0]["config"] == "intentionally invalid nginx config;"


def test_reject_unknown_node_and_cross_site_mutation(client):
    lab = client.post("/api/labs", json={"name": "test"}).json()
    url = f"/api/labs/{lab['id']}"
    body = {"kind": "load", "node_id": "169.254.169.254", "revision": 0, "request_id": "request-12345"}
    assert client.post(url + "/tasks", json=body).status_code == 422
    assert (
        client.post("/api/labs", json={"name": "x"}, headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert client.get("/api/labs", headers={"Host": "evil.example"}).status_code == 400


def test_topology_validation(client):
    lab = client.post("/api/labs", json={"name": "test"}).json()
    topology = graph()
    topology["edges"][0]["target"] = "missing"
    assert (
        client.put(
            f"/api/labs/{lab['id']}/draft", json={"base_revision": 0, "topology": topology}
        ).status_code
        == 422
    )


def test_interrupted_task_does_not_claim_success(client):
    lab = client.post("/api/labs", json={"name": "test"}).json()
    client.post(
        f"/api/labs/{lab['id']}/tasks", json={"kind": "apply", "revision": 0, "request_id": "interrupt-123"}
    )
    claim()
    recover()
    assert client.get(f"/api/labs/{lab['id']}").json()["applied_revision"] is None
    with db.connect() as con:
        assert con.execute("SELECT status FROM tasks").fetchone()[0] == "interrupted"


def test_archived_task_keeps_idempotency_receipt(client, monkeypatch):
    monkeypatch.setattr(db, "TASK_HISTORY_PER_LAB", 1)
    lab = client.post("/api/labs", json={"name": "retention"}).json()
    root = f"/api/labs/{lab['id']}"
    first = {"kind": "cleanup", "revision": 0, "request_id": "archive-first"}
    second = {"kind": "cleanup", "revision": 0, "request_id": "archive-second"}
    client.post(root + "/tasks", json=first)
    with db.connect() as con:
        first_id = con.execute("SELECT id FROM tasks WHERE request_id=?", (first["request_id"],)).fetchone()[0]
    db.finish(first_id, "succeeded", {"cleaned": True})
    client.post(root + "/tasks", json=second)
    with db.connect() as con:
        second_id = con.execute("SELECT id FROM tasks WHERE request_id=?", (second["request_id"],)).fetchone()[0]
    db.finish(second_id, "succeeded", {"cleaned": True})
    assert len(client.get(root + "/tasks").json()) == 1
    assert client.post(root + "/tasks", json=first).status_code == 410
    assert client.post(root + "/tasks", json={**first, "kind": "start_lab"}).status_code == 409


def test_worker_claims_other_labs_but_serializes_each_lab(client):
    first_lab = client.post("/api/labs", json={"name": "first"}).json()
    second_lab = client.post("/api/labs", json={"name": "second"}).json()
    for lab, request_id in ((first_lab, "first-task-1"), (first_lab, "first-task-2"), (second_lab, "second-task")):
        client.post(
            f"/api/labs/{lab['id']}/tasks",
            json={"kind": "cleanup", "revision": 0, "request_id": request_id},
        )
    one = claim()
    two = claim()
    assert one["lab_id"] != two["lab_id"]
    assert claim() is None


def test_delete_task_blocks_draft_writes(client):
    lab = client.post("/api/labs", json={"name": "delete guard"}).json()
    root = f"/api/labs/{lab['id']}"
    with db.connect() as con:
        con.execute(
            "INSERT INTO tasks(id,lab_id,request_id,kind,status,payload,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("delete-task", lab["id"], "delete-request", "delete_lab", "queued", '{}', db.now(), db.now()),
        )
    assert client.put(root + "/draft", json={"base_revision": 0, "topology": graph()}).status_code == 409
    assert client.patch(root + "/entries/fake", json={"trace": True}).status_code == 409
