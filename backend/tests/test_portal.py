from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from infra import db, portal
from infra.api import app


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("INFRA_DATA", str(tmp_path))
    with TestClient(app) as control, TestClient(portal.app) as browser:
        lab = control.post("/api/labs", json={"name": "portal"}).json()
        with db.connect() as con:
            con.execute(
                "UPDATE labs SET applied=? WHERE id=?",
                ('{"nodes":[{"id":"gateway","kind":"nginx"}]}', lab["id"]),
            )
        entry = control.post(f"/api/labs/{lab['id']}/entries", json={"node_id": "gateway"}).json()
        yield control, browser, lab["id"], entry


def test_entry_lifecycle_and_host_boundary(clients, monkeypatch):
    control, browser, lab, entry = clients
    resolve = Mock()
    monkeypatch.setattr(portal, "resolve", resolve)
    url = f"/api/labs/{lab}/entries"
    assert control.post(url, json={"node_id": "gateway"}).json()["id"] == entry["id"]
    assert control.post(url, json={"node_id": "127.0.0.1"}).status_code == 422
    assert browser.get("/", headers={"host": "127.0.0.1:8089"}).status_code == 404
    assert browser.get("/", headers={"host": "evil.example"}).status_code == 404
    assert control.patch(url + "/" + entry["id"], json={"trace": True}).status_code == 200
    control.delete(url + "/" + entry["id"])
    assert browser.get("/", headers={"host": entry["hostname"] + ":8089"}).status_code == 410
    resolve.assert_not_called()


def test_post_stream_cookie_redirect_and_no_sensitive_storage(clients, monkeypatch):
    control, browser, lab, entry = clients
    runtime, target = Mock(), Mock()
    target.name = "internal-gateway"
    monkeypatch.setattr(portal, "resolve", lambda _: (runtime, target, [], "172.30.0.2", {}, {}))
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(
            302,
            headers=[
                ("location", "http://gateway:8080/details?q=1"),
                ("set-cookie", "session=secret; Domain=localhost; Path=/; HttpOnly"),
                ("set-cookie", "other=value; Path=/"),
                ("x-request-id", "spoof"),
            ],
            stream=httpx.ByteStream(b"actual body"),
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        portal.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    host = entry["hostname"] + ":8089"
    response = browser.post(
        "/submit?q=one",
        content=b"message=private",
        headers={
            "host": host,
            "Cookie": "incoming=private",
            "Authorization": "Bearer private",
            "X-Request-ID": "spoof",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.content == b"actual body"
    assert response.headers["location"] == f"http://{host}/details?q=1"
    assert all("domain=" not in cookie.lower() for cookie in response.headers.get_list("set-cookie"))
    assert len(response.headers.get_list("set-cookie")) == 2
    assert captured[0].method == "POST" and captured[0].content == b"message=private"
    assert captured[0].headers["host"] == host
    assert captured[0].headers["x-request-id"] != "spoof"
    records = control.get(f"/api/labs/{lab}/requests").json()
    assert records[0]["source"] == "browser" and records[0]["method"] == "POST"
    detail = control.get(f"/api/labs/{lab}/requests/{records[0]['id']}").json()
    assert detail["events"] == []
    assert "private" not in str(detail) and "secret" not in str(detail)
    assert detail["result"]["body"] is None
    assert not control.get(f"/api/labs/{lab}/tasks").json()


def test_body_limit_and_stopped_node(clients, monkeypatch):
    control, browser, lab, entry = clients
    resolve = Mock(side_effect=RuntimeError("节点已停止"))
    monkeypatch.setattr(portal, "resolve", resolve)
    headers = {"host": entry["hostname"] + ":8089"}
    assert browser.post("/submit", content=b"x" * 1048577, headers=headers).status_code == 413
    resolve.assert_not_called()
    assert browser.get("/", headers=headers).status_code == 503
    assert len(control.get(f"/api/labs/{lab}/requests").json()) == 2
