import json
from unittest.mock import Mock

import pytest

from infra.runtime import Runtime, filters, labels, projected_peak_memory


def runtime_with_mock():
    runtime = object.__new__(Runtime)
    runtime.client = Mock()
    return runtime


def test_cleanup_uses_both_ownership_labels(tmp_path, monkeypatch):
    from infra import db

    monkeypatch.setenv("INFRA_DATA", str(tmp_path))
    db.init()
    runtime = runtime_with_mock()
    container, network = Mock(), Mock()
    runtime.client.containers.list.return_value = [container]
    runtime.client.networks.list.return_value = [network]
    runtime.cleanup("lab-a", lambda _: None)
    runtime.client.containers.list.assert_called_once_with(all=True, filters=filters("lab-a"))
    runtime.client.networks.list.assert_called_once_with(filters=filters("lab-a"))
    assert filters("lab-a")["label"] == ["infra.owner=infra-lab-v1", "infra.lab=lab-a"]
    container.remove.assert_called_once_with(force=True)
    network.remove.assert_called_once()


def test_reject_foreign_network_name_collision():
    runtime = runtime_with_mock()
    runtime.client.networks.get.return_value.attrs = {"Labels": labels("other-lab")}
    with pytest.raises(RuntimeError, match="冲突"):
        runtime.network("lab-a")


def test_invalid_nginx_config_does_not_reload_existing_container():
    runtime = runtime_with_mock()
    runtime.containers = Mock(return_value=[])
    runtime.client.containers.list.return_value = []
    runtime.validate = Mock(side_effect=RuntimeError("配置校验失败"))
    runtime.create = Mock()
    runtime.reload = Mock()
    with pytest.raises(RuntimeError):
        runtime.apply(
            "lab-a", {"nodes": [{"id": "gateway", "kind": "nginx", "memory_mb": 64}]}, "task", lambda _: None
        )
    runtime.create.assert_not_called()
    runtime.reload.assert_not_called()


def test_topology_quota_counts_unmatched_old_nodes_and_validator_memory():
    old = Mock()
    old.labels = {"infra.node": "removed-app", "infra.role": "node"}
    old.attrs = {"HostConfig": {"Memory": 128 * 1048576}, "Config": {"Image": "app:old"}}
    topology = [
        {"id": "app-new", "kind": "app", "memory_mb": 128, "cpu": 0.5, "delay_ms": 0, "error_rate": 0},
        {"id": "gateway", "kind": "nginx", "memory_mb": 64, "cpu": 0.5, "config": ""},
    ]
    assert projected_peak_memory([old], topology, lambda kind: f"{kind}:current") == 320 * 1048576


def test_apply_rejects_peak_memory_before_nginx_validator(monkeypatch):
    monkeypatch.setenv("INFRA_TOTAL_MEMORY_MB", "768")
    runtime = runtime_with_mock()
    external, old = Mock(), Mock()
    external.labels = {"infra.lab": "other"}
    external.attrs = {"HostConfig": {"Memory": 600 * 1048576}}
    old.labels = {"infra.lab": "lab-a", "infra.node": "removed", "infra.role": "node"}
    old.attrs = {"HostConfig": {"Memory": 128 * 1048576}, "Config": {"Image": "app:old"}}
    runtime.client.containers.list.return_value = [external, old]
    runtime.containers = Mock(return_value=[old])
    runtime.image_for = lambda kind: f"{kind}:current"
    runtime.validate = Mock()
    with pytest.raises(RuntimeError, match="内存配额"):
        runtime.apply(
            "lab-a",
            {"nodes": [
                {"id": "app-new", "kind": "app", "memory_mb": 128, "cpu": 0.5, "delay_ms": 0, "error_rate": 0},
                {"id": "gateway", "kind": "nginx", "memory_mb": 64, "cpu": 0.5, "config": ""},
            ]},
            "task",
            lambda _: None,
        )
    runtime.validate.assert_not_called()


def test_forget_container_history_is_lab_scoped(tmp_path, monkeypatch):
    from infra import db

    monkeypatch.setenv("INFRA_DATA", str(tmp_path))
    db.init()
    container = Mock(id="deadbeef")
    with db.connect() as con:
        con.execute("INSERT INTO meta VALUES(?,?)", ("node-ip-v2:lab-a:deadbeef", '{}'))
        con.execute("INSERT INTO meta VALUES(?,?)", ("node-ip-v2:lab-b:deadbeef", '{}'))
        con.execute("INSERT INTO meta VALUES(?,?)", ("node-ip:deadbeef", '{}'))
    Runtime.forget_container_history("lab-a", container)
    with db.connect() as con:
        keys = {r[0] for r in con.execute("SELECT key FROM meta")}
    assert keys == {"node-ip-v2:lab-b:deadbeef"}


def test_legacy_ip_history_is_namespaced_or_discarded(tmp_path, monkeypatch):
    from infra import db

    monkeypatch.setenv("INFRA_DATA", str(tmp_path))
    db.init()
    runtime = runtime_with_mock()
    current = Mock(id="abc123", labels={"infra.lab": "lab-a", "infra.role": "node"})
    runtime.client.containers.list.return_value = [current]
    with db.connect() as con:
        con.execute("INSERT INTO meta VALUES(?,?)", ("node-ip:abc123", '{"ip":"172.20.0.2","time":1}'))
        con.execute("INSERT INTO meta VALUES(?,?)", ("node-ip:removed", '{"ip":"172.20.0.3","time":1}'))
    runtime.migrate_node_ip_history()
    with db.connect() as con:
        values = dict(con.execute("SELECT key,value FROM meta"))
    assert "node-ip:abc123" not in values and "node-ip:removed" not in values
    assert json.loads(values["node-ip-v2:lab-a:abc123"]) == {"ip": "172.20.0.2", "time": 1}
