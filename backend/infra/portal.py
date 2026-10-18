"""Isolated browser ingress. Linux host networking, loopback listener only.

Targets are resolved from owned Docker containers, never from browser URLs.
Browser traffic never enters the deployment task queue.
"""

import asyncio
import ipaddress
import json
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from . import db
from .runtime import Runtime, node_ip_key

HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
collectors = {}
collector_tasks = {}
slots = asyncio.Semaphore(16)
runtime_lock = threading.Lock()
shared_runtime = None


@asynccontextmanager
async def lifespan(app):
    global shared_runtime
    db.init()
    with db.connect() as con:
        interrupted = [
            r[0]
            for r in con.execute(
                "SELECT r.id FROM client_requests r JOIN browser_requests b ON r.id=b.request_id WHERE r.status='running'"
            )
        ]
    for request_id in interrupted:
        db.request_event(
            request_id, "portal", "worker.interrupted", data={"message": "网站入口采集器重启，未自动重发"}
        )
        db.request_status(
            request_id, "interrupted", {"error": "网站入口采集器中断；应用状态未知", "trace_complete": False}
        )
    app.state.http_client = None
    yield
    for task in list(collector_tasks.values()):
        task.cancel()
    await asyncio.gather(*collector_tasks.values(), return_exceptions=True)
    if app.state.http_client:
        await app.state.http_client.aclose()
    with runtime_lock:
        if shared_runtime:
            shared_runtime.close()
            shared_runtime = None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def resolve(entry):
    global shared_runtime
    with runtime_lock:
        if shared_runtime is None:
            shared_runtime = Runtime()
        runtime = shared_runtime
    nodes = [c for c in runtime.containers(entry["lab_id"]) if c.labels.get("infra.role") == "node"]
    matches = [c for c in nodes if c.labels.get("infra.node") == entry["node_id"]]
    if len(matches) != 1:
        raise RuntimeError(f"入口目标对应 {len(matches)} 个容器")
    target = matches[0]
    target.reload()
    if target.labels.get("infra.kind") != "nginx" or target.status != "running":
        raise RuntimeError("Nginx 已停止或不可用，请在工作台启动实验")
    ips, historical = {}, {}
    keys = [node_ip_key(entry["lab_id"], c.id) for c in nodes]
    legacy = [f"node-ip:{c.id}" for c in nodes]
    with db.connect() as con:
        values = {
            row[0]: json.loads(row[1])
            for row in con.execute(
                "SELECT key,value FROM meta WHERE key IN (" + ",".join("?" for _ in keys + legacy) + ")",
                keys + legacy,
            )
        } if keys else {}
    for c in nodes:
        ip = c.attrs["NetworkSettings"]["Networks"].get(f"il-{entry['lab_id']}", {}).get("IPAddress")
        if ip:
            ips[ip] = c.labels["infra.node"]
        previous = values.get(node_ip_key(entry["lab_id"], c.id)) or values.get(f"node-ip:{c.id}")
        if previous:
            historical[previous["ip"]] = c.labels["infra.node"]
    ip = target.attrs["NetworkSettings"]["Networks"].get(f"il-{entry['lab_id']}", {}).get("IPAddress")
    if not ip or not ipaddress.ip_address(ip).is_private:
        raise RuntimeError("无法确认实验网络地址")
    return runtime, target, nodes, ip, ips, historical


def response_headers(headers, public_host, internal_hosts):
    connection = {x.strip().lower() for x in headers.get("connection", "").split(",")}
    result = []
    for key, value in headers.multi_items():
        lower = key.lower()
        if lower in HOP | connection or lower in {
            "access-control-allow-origin",
            "access-control-allow-credentials",
            "x-request-id",
        }:
            continue
        if lower == "set-cookie":
            # Domain cookies must not leak to sibling experiment origins.
            value = re.sub(r";\s*domain=[^;]*", "", value, flags=re.I)
        if lower == "location":
            parsed = urlsplit(value)
            if parsed.hostname in internal_hosts:
                value = urlunsplit(("http", public_host, parsed.path, parsed.query, parsed.fragment))
        result.append((key.encode("latin-1"), value.encode("latin-1")))
    return result


class Trace:
    def __init__(self, request_id, resources):
        self.id = request_id
        self.runtime, self.target, self.nodes, self.ip, self.ips, self.historical = resources
        self.started = time.monotonic()
        self.received, self.finished = set(), set()
        self.nginx = False
        self.done = asyncio.Event()
        self.result = {}

    def accept(self, container, event):
        if event.get("request_id") != self.id:
            return
        source = container.labels["infra.kind"]
        node_id = container.labels["infra.node"]
        if source == "nginx" and "upstream" in event:
            self.nginx = True
            statuses = str(event.get("upstream_status", "")).split(",")
            attempts = []
            for i, address in enumerate(str(event["upstream"]).split(",")):
                address = address.strip()
                if address in ("", "-"):
                    continue
                ip = address.rsplit(":", 1)[0]
                attempts.append(
                    {
                        "address": address,
                        "node_id": self.ips.get(ip) or self.historical.get(ip),
                        "mapping": "current" if ip in self.ips else "last_observed",
                        "status": statuses[i].strip() if i < len(statuses) else "unknown",
                    }
                )
            db.request_event(
                self.id, source, "nginx.completed", node_id, "posthoc",
                data={"attempts": attempts, "status": event.get("status"), "response_version": event.get("response_version")},
            )
        elif source == "app" and event.get("infra_trace") == 1:
            kind = event.get("kind")
            if kind not in {"app.received", "app.processing", "app.response_written", "app.response_aborted"}:
                return
            attempt = (node_id, event.get("attempt_id"))
            if kind == "app.received":
                self.received.add(attempt)
            if kind in {"app.response_written", "app.response_aborted"}:
                self.finished.add(attempt)
            db.request_event(
                self.id, source, kind, node_id, timestamp=event.get("time"),
                data={"attempt_id": event.get("attempt_id"), "status": event.get("status"), "response_version": event.get("response_version")},
            )

    async def finish(self, interrupted=False):
        complete = self.nginx and bool(self.received) and self.received <= self.finished
        self.result.update(
            trace_complete=complete,
            trace_note="日志已关联；路径为事后还原" if complete else "链路不完整；未观测步骤保持未知",
        )
        status = "interrupted" if interrupted else "completed"
        await asyncio.to_thread(db.request_status, self.id, status, self.result)


class LabTraceCollector:
    def __init__(self, lab_id):
        self.lab_id = lab_id
        self.traces = {}
        self.seen = set()
        self.seen_order = deque()
        self.since = max(0, int(time.time()) - 2)

    def poll(self):
        active = list(self.traces.values())
        nodes = {c.id: c for trace in active for c in trace.nodes}
        for container in nodes.values():
            try:
                lines = container.logs(since=self.since, tail=2000).decode(errors="replace").splitlines()
            except Exception:
                continue
            for line in lines:
                key = (container.id, line)
                if key in self.seen:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                request_id = event.get("request_id")
                trace = self.traces.get(request_id)
                if not trace:
                    continue
                trace.accept(container, event)
                self.seen.add(key)
                self.seen_order.append(key)
        while len(self.seen_order) > 10000:
            self.seen.discard(self.seen_order.popleft())
        self.since = max(0, int(time.time()) - 2)

    async def run(self):
        deadlines, completed_at = {}, {}
        try:
            while self.traces:
                now = time.monotonic()
                try:
                    await asyncio.to_thread(self.poll)
                except Exception as exc:
                    for trace in self.traces.values():
                        trace.result.update(trace_complete=False, trace_note=f"采集暂不可用：{exc}")
                for request_id, trace in list(self.traces.items()):
                    deadlines.setdefault(request_id, now + 45)
                    if trace.done.is_set():
                        completed_at.setdefault(request_id, now)
                    if now >= deadlines[request_id] or (
                        request_id in completed_at
                        and now - completed_at[request_id] > 1
                        and trace.received <= trace.finished
                    ):
                        try:
                            await trace.finish()
                        except Exception as exc:
                            trace.result.update(trace_complete=False, trace_note=f"保存链路失败：{exc}")
                        else:
                            self.traces.pop(request_id, None)
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            for trace in self.traces.values():
                await asyncio.to_thread(db.request_event, trace.id, "portal", "worker.interrupted")
                trace.result.update(error="采集器中断；应用状态未知", trace_complete=False)
                await trace.finish(interrupted=True)
            raise
        finally:
            collectors.pop(self.lab_id, None)
            collector_tasks.pop(self.lab_id, None)


def register_trace(trace):
    lab_id = trace.target.labels.get("infra.lab")
    collector = collectors.get(lab_id)
    if collector is None:
        collector = LabTraceCollector(lab_id)
        collectors[lab_id] = collector
        collector_tasks[lab_id] = asyncio.create_task(collector.run())
    collector.traces[trace.id] = trace


def active_entry(hostname):
    with db.connect() as con:
        row = con.execute("SELECT * FROM entries WHERE hostname=? AND active=1", (hostname,)).fetchone()
    return dict(row) if row else None


def reserve_request(entry, request_id, relative, method):
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if (
            not con.execute("SELECT 1 FROM entries WHERE id=? AND active=1", (entry["id"],)).fetchone()
            or db.deleting(con, entry["lab_id"])
        ):
            return None
        con.execute(
            "INSERT INTO client_requests VALUES(?,?,?,?,?,?,NULL,?,?)",
            (
                request_id, entry["lab_id"], entry["node_id"], relative,
                entry["trace"], "running", db.now(), db.now(),
            ),
        )
        con.execute("INSERT INTO browser_requests VALUES(?,?,?)", (request_id, entry["id"], method))
        lab = con.execute("SELECT applied_revision FROM labs WHERE id=?", (entry["lab_id"],)).fetchone()
        return True, lab[0] if lab else None


async def http_client():
    client = app.state.http_client
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=30,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        app.state.http_client = client
    return client


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str):
    host = request.headers.get("host", "").lower()
    hostname = host.split(":", 1)[0]
    if not re.fullmatch(r"[0-9a-f]{32}\.localhost(?::8089)?", host):
        return PlainTextResponse("网站入口不存在，请从工作台打开网站", status_code=404)
    entry = await asyncio.to_thread(active_entry, hostname)
    if not entry:
        return PlainTextResponse("入口已撤销或不存在", status_code=410)
    if request.headers.get("upgrade"):
        return PlainTextResponse("暂不支持协议升级", status_code=400)
    if slots.locked():
        return PlainTextResponse("网站入口并发已满，请稍后重试", status_code=429)
    await slots.acquire()
    held, transferred, finalized = True, False, False
    resources, upstream, trace = None, None, None
    request_id = db.uid()
    started = time.monotonic()
    result = {"status_code": None, "body": None, "trace_complete": None}
    raw_path = request.scope["raw_path"].decode("ascii")
    query = request.scope["query_string"].decode("ascii")
    relative = raw_path + ("?" + query if query else "")
    def release_slot():
        nonlocal held
        if held:
            held = False
            slots.release()

    async def finish():
        nonlocal finalized
        if finalized:
            return
        finalized = True
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        try:
            if upstream:
                await upstream.aclose()
            if trace:
                trace.result.update(result)
                trace.done.set()
            else:
                await asyncio.to_thread(db.request_status, request_id, "completed", result)
        finally:
            release_slot()

    try:
        reservation = await asyncio.to_thread(reserve_request, entry, request_id, relative, request.method)
        if reservation is None:
            return PlainTextResponse("入口已撤销或实验正在删除", status_code=410)
        _, applied_revision = reservation
        result["applied_revision"] = applied_revision
        client = await http_client()
        async with asyncio.timeout(30):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1048576:
                    result.update(status_code=413, error="请求体超过 1 MB")
                    await finish()
                    return PlainTextResponse(result["error"], status_code=413)
            resources = await asyncio.to_thread(resolve, entry)
            if entry["trace"]:
                trace = Trace(request_id, resources)
                register_trace(trace)
            connection = {x.strip().lower() for x in request.headers.get("connection", "").split(",")}
            blocked = (
                HOP
                | connection
                | {
                    "host",
                    "content-length",
                    "x-request-id",
                    "x-infra-trace",
                    "forwarded",
                    "x-forwarded-for",
                    "x-forwarded-host",
                    "x-forwarded-proto",
                }
            )
            headers = [(k, v) for k, v in request.headers.items() if k.lower() not in blocked]
            headers += [("host", host), ("x-request-id", request_id)]
            if entry["trace"]:
                headers.append(("x-infra-trace", "1"))
            await asyncio.to_thread(db.request_event, request_id, "portal", "client.sent")
            outgoing = client.build_request(
                request.method, f"http://{resources[3]}:8080{relative}", headers=headers, content=bytes(body)
            )
            upstream = await client.send(outgoing, stream=True)
            result.update(
                status_code=upstream.status_code, content_type=upstream.headers.get("content-type", "")
            )
            await asyncio.to_thread(db.request_event, request_id, "portal", "client.headers")

        async def stream():
            try:
                async with asyncio.timeout(max(0.01, 30 - (time.monotonic() - started))):
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                await asyncio.to_thread(db.request_event, request_id, "portal", "client.response")
            except asyncio.CancelledError:
                result.update(error="浏览器连接已断开；不表示应用取消", error_kind="disconnect")
                await asyncio.to_thread(db.request_event, request_id, "portal", "client.failed")
                raise
            except Exception as exc:
                result.update(
                    error=str(exc) or "响应流超时",
                    error_kind="timeout"
                    if isinstance(exc, (TimeoutError, httpx.TimeoutException))
                    else "connection",
                )
                await asyncio.to_thread(db.request_event, request_id, "portal", "client.failed")
                raise
            finally:
                await asyncio.shield(finish())

        response = StreamingResponse(stream(), status_code=upstream.status_code)
        internal = {
            resources[3], entry["node_id"], resources[1].name.lstrip("/"), "backends",
            *resources[4].keys(), *resources[5].keys(),
        }
        for container in resources[2]:
            internal.add(container.name.lstrip("/"))
            internal.add(container.labels.get("infra.node", ""))
        response.raw_headers = response_headers(upstream.headers, host, internal)
        response.raw_headers += [(b"x-request-id", request_id.encode()), (b"referrer-policy", b"same-origin")]
        transferred = True
        return response
    except asyncio.CancelledError:
        result.update(error="浏览器连接已断开；不表示应用取消", error_kind="disconnect")
        await asyncio.to_thread(db.request_event, request_id, "portal", "client.failed")
        await asyncio.shield(finish())
        raise
    except Exception as exc:
        status = 504 if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else 503
        result.update(
            status_code=status,
            error=str(exc) or "入口请求超时",
            error_kind="timeout" if status == 504 else "connection",
        )
        await asyncio.to_thread(db.request_event, request_id, "portal", "client.failed")
        await finish()
        return PlainTextResponse(
            f"网站入口不可用：{result['error']}", status_code=status, headers={"X-Request-ID": request_id}
        )
    finally:
        if not transferred:
            release_slot()
