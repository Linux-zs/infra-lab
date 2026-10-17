"""A single real GET. One response is captured for both preview and details."""

import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

host, path, request_id, trace = sys.argv[1:]
start = time.monotonic()
lock = threading.Lock()
done = threading.Event()
result = {
    "request_id": request_id,
    "status_code": None,
    "headers": [],
    "body": "",
    "content_type": "",
    "truncated": False,
    "error": None,
    "error_kind": None,
}
parts = []


def event(kind):
    if trace == "1":
        print(
            json.dumps(
                {
                    "infra_trace": 1,
                    "kind": kind,
                    "request_id": request_id,
                    "time": time.time(),
                }
            ),
            flush=True,
        )


def send():
    connection = http.client.HTTPConnection(host, 8080, timeout=10)
    try:
        headers = {"X-Request-ID": request_id, "Accept-Encoding": "identity"}
        if trace == "1":
            headers["X-Infra-Trace"] = "1"
        event("client.sent")
        # Preserve existing URL escapes and encode Unicode without changing the target host.
        connection.request("GET", quote(path, safe="/%?=&:+,;@!$'()*~-._"), headers=headers)
        response = connection.getresponse()
        with lock:
            result.update(
                status_code=response.status,
                headers=response.getheaders(),
                content_type=response.getheader("Content-Type", ""),
            )
        event("client.headers")
        size = 0
        while size <= 1048576:
            chunk = response.read1(min(16384, 1048577 - size))
            if not chunk:
                break
            with lock:
                parts.append(chunk)
            size += len(chunk)
        with lock:
            result["truncated"] = size > 1048576
        event("client.response")
    except Exception as exc:
        with lock:
            result.update(
                error=str(exc),
                error_kind="timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "connection",
            )
        event("client.failed")
    finally:
        connection.close()
        done.set()


threading.Thread(target=send, daemon=True).start()
if not done.wait(10):
    with lock:
        result.update(error="客户端总时限 10 秒已到；不表示应用已停止处理", error_kind="timeout")
    event("client.timeout")
with lock:
    raw = b"".join(parts)[:1048576]
    from email.message import Message

    message = Message()
    message["content-type"] = result["content_type"]
    charset = message.get_content_charset() or "utf-8"
    try:
        result["body"] = raw.decode(charset, errors="replace")
    except LookupError:
        result["body"] = raw.decode("utf-8", errors="replace")
    result["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
    result["bytes_captured"] = len(raw)
    Path("/tmp/response.json").write_text(json.dumps(result), encoding="utf-8")
