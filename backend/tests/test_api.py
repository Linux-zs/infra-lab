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
