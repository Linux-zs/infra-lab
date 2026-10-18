from email.message import Message

import pytest

from infra.load_identity import response_node


@pytest.mark.parametrize("marker,body,expected", [
    ("app-a", b"<h1>website</h1>", "app-a"),
    ("app-b", b'{"node":"business-value"}', "app-b"),
    (None, b'{"node":"legacy-app"}', "legacy-app"),
    (None, b"<h1>nginx error</h1>", None),
    (None, b'["not-an-instance"]', None),
    (None, b'{"node":123}', None),
    (None, b'null', None),
])
def test_identity_header_preferred_and_missing_identity_remains_unknown(marker, body, expected):
    headers = Message()
    if marker is not None:
        headers["X-Infra-Node"] = marker
    assert response_node(headers, body) == expected
