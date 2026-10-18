from unittest.mock import Mock

import pytest

from infra.models import Action, Topology, normalize
from infra.redis_runtime import command, redis_config, replication
from infra.runtime import fingerprint


def graph(edges):
    return {"nodes": [{"id": i, "kind": "redis", "position": {"x": 0, "y": 0}}
                      for i in ["a", "b", "c"]], "edges": edges}


@pytest.mark.parametrize("edges", [
    [{"source": "a", "target": "a"}],
    [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
    [{"source": "a", "target": "c"}, {"source": "b", "target": "c"}],
])
def test_replication_rejects_cycles_and_multiple_sources(edges):
    with pytest.raises(ValueError):
        Topology.model_validate(graph(edges))


def test_replication_is_derived_from_graph_and_does_not_rebuild_container():
    data = graph([{"source": "a", "target": "b"}])
    data["nodes"][1]["replica_of"] = "outside"
    normalized = normalize(Topology.model_validate(data))
    primary, replica = normalized["nodes"][:2]
    assert replica["replica_of"] == "a"
    assert primary["replica_of"] is None
    assert "replicaof a 6379" in redis_config(replica)
    changed = {**replica, "replica_of": "c"}
    assert fingerprint(changed) == fingerprint(replica)


def test_command_uses_literal_arguments_and_preserves_null():
    runtime = Mock()
    node = runtime.node.return_value
    node.labels = {"infra.kind": "redis"}
    node.status = "running"
    node.exec_run.return_value = Mock(exit_code=0, output=b"null\n")
    action = Action(request_id="request-1", kind="redis_command", revision=1,
                    node_id="a", redis_op="GET", key="key; $(do not execute)").model_dump()
    assert command(runtime, "lab-a", action)["value"] is None
    node.exec_run.assert_called_once_with(
        ["timeout", "4", "redis-cli", "--json", "-e", "GET", "key; $(do not execute)"])
    node.exec_run.return_value = Mock(exit_code=1, output=b"READONLY replica")
    with pytest.raises(RuntimeError, match="READONLY"):
        command(runtime, "lab-a", {**action, "redis_op": "SET"})


def test_observation_preserves_unknown_and_raw_replication_fields():
    node = Mock()
    node.exec_run.return_value = Mock(exit_code=0, output=
        b"# Replication\r\nrole:slave\r\nmaster_link_status:down\r\nslave_repl_offset:125\r\n")
    assert replication(node)["fields"]["master_link_status"] == "down"
    node.exec_run.side_effect = RuntimeError("stopped")
    result = replication(node)
    assert result["available"] is False and "fields" not in result


def test_arbitrary_commands_rejected():
    with pytest.raises(ValueError):
        Action(request_id="request-1", kind="redis_command", revision=1, redis_op="CONFIG")
