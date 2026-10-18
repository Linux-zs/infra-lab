import importlib.util
import json
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from infra import db, response_config
from infra.api import app
from infra.response_config import DEFAULT, validate


@pytest.mark.parametrize("patch", [
    {"body": "{"}, {"body": "NaN"}, {"body": "中" * 22000, "format": "text"},
    {"status": 204}, {"status": 205}, {"status": 304},
    {"headers": [{"name": "X-Demo", "value": "a\r\nb"}]},
    {"headers": [{"name": "Content-Length", "value": "9"}]},
    {"headers": [{"name": "X-Infra-Node", "value": "fake"}]},
    {"headers": [{"name": "X", "value": "a" * 8192}]},
])
def test_invalid_config_rejected(patch):
    with pytest.raises(ValueError):
        validate({**DEFAULT, "mode": "custom", **patch})


def test_saved_response_conflict_and_frozen_task(tmp_path, monkeypatch):
    monkeypatch.setenv("INFRA_DATA", str(tmp_path))
    with TestClient(app) as client:
        lab = client.post("/api/labs", json={"name": "responses"}).json()
        root = f"/api/labs/{lab['id']}"
        topology = {"nodes": [{"id": "app-a", "kind": "app", "position": {"x": 0, "y": 0}}], "edges": []}
        client.put(root + "/draft", json={"base_revision": 0, "topology": topology})
        with db.connect() as con:
            con.execute("UPDATE labs SET applied=?,applied_revision=1 WHERE id=?", (json.dumps(topology), lab["id"]))
        path = root + "/nodes/app-a/response"
        assert client.put(path, json={"base_version": 0, "config": DEFAULT}).status_code == 200
        assert client.put(path, json={"base_version": 0, "config": DEFAULT}).status_code == 409
        body = {"request_id": "response-1", "kind": "apply_response", "revision": 0, "node_id": "app-a", "response_version": 1}
        task = client.post(root + "/tasks", json=body).json()
        assert task["payload"]["response"]["version"] == 1
        client.put(path, json={"base_version": 1, "config": {**DEFAULT, "mode": "custom"}})
        assert client.post(root + "/tasks", json=body).json()["id"] == task["id"]
        assert client.get(root).json()["revision"] == 1
        assert client.get(path).json()["version"] == 2


def test_real_application_snapshot_methods_and_health(monkeypatch):
    # Execute the same server module shipped in the application image.
    monkeypatch.setitem(sys.modules, "response_config", response_config)
    spec = importlib.util.spec_from_file_location("lab_test_app", Path(__file__).parents[2] / "services/app/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    active = {"version": 1, "digest": "test", "config": {**DEFAULT, "mode": "custom", "format": "text", "body": "原响应", "delay_ms": 200,
        "headers": [{"name": "Set-Cookie", "value": "a=1"}, {"name": "Set-Cookie", "value": "b=2"}]}}
    started = threading.Event()
    def snapshot():
        value = active.copy()
        started.set()
        return value
    monkeypatch.setattr(module, "response_snapshot", snapshot)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def request(method="GET", path="/anything"):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        conn.request(method, path)
        response = conn.getresponse()
        value = response.status, response.getheaders(), response.read()
        conn.close()
        return value
    result = []
    pending = threading.Thread(target=lambda: result.append(request()))
    try:
        pending.start()
        assert started.wait(1)
        active.update(version=2, config={**active["config"], "body": "新响应", "delay_ms": 0})
        pending.join()
        assert result[0][2].decode() == "原响应"
        assert dict(result[0][1])["X-Infra-Response-Version"] == "1"
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            status, headers, body = request(method)
            assert status == 200 and body.decode() == "新响应"
            assert len([h for h in headers if h[0] == "Set-Cookie"]) == 2
        assert request("HEAD")[2] == b""
        health = json.loads(request(path="/__infra_health")[2])
        assert health["health"] == "ok" and health["response_version"] == 2
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
