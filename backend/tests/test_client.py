import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from infra import db
from infra.api import app, request_events
from infra.models import Action
from infra.runtime import Runtime, filters
from infra.worker import claim, recover


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INFRA_DATA", str(tmp_path))
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "path", ["http://evil/", "//evil", "/%2fexample.com", "/\\evil", "/x#fragment", "/%0d%0aHost:evil"]
)
def test_reject_external_or_ambiguous_target(path):
    with pytest.raises(ValueError):
        Action(kind="client_request", request_id="test-request-id", revision=0, path=path)


def deployed(client):
    lab = client.post("/api/labs", json={"name": "test"}).json()
    with db.connect() as con:
        con.execute(
            "UPDATE labs SET applied=? WHERE id=?",
            (json.dumps({"nodes": [{"id": "gateway", "kind": "nginx"}]}), lab["id"]),
        )
    return lab["id"]


def test_request_dedup_cursor_trace_off_and_recovery(client):
    lab_id = deployed(client)
    url = f"/api/labs/{lab_id}"
    action = {
        "kind": "client_request",
        "request_id": "request-client-1",
        "revision": 0,
        "node_id": "gateway",
        "path": "/page?a=1",
        "trace": True,
    }
    task = client.post(url + "/tasks", json=action).json()
    assert client.post(url + "/tasks", json=action).json()["id"] == task["id"]
    assert len(client.get(url + "/requests").json()) == 1
    db.request_event(task["id"], "client", "client.sent")
    first = request_events(lab_id)[0]
    db.request_event(task["id"], "app", "app.processing", "app-1")
    assert len(request_events(lab_id, first["seq"])) == 1
    assert not request_events("other-lab")
    claim()
    recover()
    detail = client.get(url + "/requests/" + task["id"]).json()
    assert detail["status"] == "interrupted"
    assert detail["events"][-1]["kind"] == "worker.interrupted"
    off = client.post(
        url + "/tasks", json={**action, "request_id": "request-client-2", "trace": False}
    ).json()
    db.request_event(off["id"], "client", "client.sent")
    assert client.get(url + "/requests/" + off["id"]).json()["events"] == []
    assert client.get("/api/labs/other/requests/" + task["id"]).status_code == 404


def test_additive_migration_and_retention(client):
    lab_id = deployed(client)
    db.init()
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM labs").fetchone()[0] == 1
        for i in range(102):
            con.execute(
                "INSERT INTO client_requests VALUES(?,?,?,?,?,?,NULL,?,?)",
                (str(i), lab_id, "gateway", "/", 1, "completed", i, i),
            )
        con.execute(
            "INSERT INTO client_requests VALUES('queued',?,?,?,?,?,NULL,?,?)",
            (lab_id, "gateway", "/", 1, "queued", -1, -1),
        )
    db.request_event("0", "client", "client.sent")
    db.request_status("101", "completed", {"body": "ok"})
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM client_requests").fetchone()[0] == 101
        assert not con.execute("SELECT * FROM request_events WHERE request_id='0'").fetchone()
        assert con.execute("SELECT * FROM client_requests WHERE id='queued'").fetchone()


def test_lifecycle_order_idempotency_partial_failure():
    runtime = object.__new__(Runtime)
    runtime.client = Mock()
    order = []
    containers = []
    for name, kind in [("gateway", "nginx"), ("app-1", "app"), ("app-2", "app")]:
        container = Mock()
        container.labels = {"infra.node": name, "infra.kind": kind, "infra.role": "node"}
        container.status = "exited"
        container.start.side_effect = lambda n=name: order.append(n)
        container.stop.side_effect = lambda timeout, n=name: order.append(n)
        containers.append(container)
    runtime.client.containers.list.return_value = containers
    runtime.ready = Mock()
    assert not runtime.lifecycle("one", True, lambda _: None)["partial_failure"]
    assert order == ["app-1", "app-2", "gateway"]
    runtime.client.containers.list.assert_called_with(all=True, filters=filters("one"))
    order.clear()
    for c in containers:
        c.status = "running"
    assert all(n["status"] == "skipped" for n in runtime.lifecycle("one", True, lambda _: None)["nodes"])
    assert order == []
    containers[1].stop.side_effect = RuntimeError("unavailable")
    result = runtime.lifecycle("one", False, lambda _: None)
    assert result["partial_failure"]
    assert order == ["gateway", "app-2"]
    runtime.client.containers.list.return_value = []
    with pytest.raises(RuntimeError, match="应用拓扑"):
        runtime.lifecycle("one", True, lambda _: None)
