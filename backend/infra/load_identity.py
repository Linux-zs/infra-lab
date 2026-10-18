"""Response identity survives HTML/text/custom bodies; old JSON apps still work."""

import json


def response_node(headers, body):
    marker = headers.get("X-Infra-Node")
    if marker:
        return marker
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    marker = value.get("node") if isinstance(value, dict) else None
    return marker if isinstance(marker, str) and marker else None
