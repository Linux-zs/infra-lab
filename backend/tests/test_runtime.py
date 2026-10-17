from unittest.mock import Mock

import pytest

from infra.runtime import Runtime, filters, labels


def runtime_with_mock():
    runtime = object.__new__(Runtime)
    runtime.client = Mock()
    return runtime


def test_cleanup_uses_both_ownership_labels():
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
