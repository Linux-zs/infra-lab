import html
import json
import os
import random
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.handle_request(head=True)

    def do_POST(self):
        self.handle_request()

    def do_GET(self):
        self.handle_request()

    def handle_request(self, head=False):
        payload = b""
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1048576:
                self.send_error(413)
                return
            payload = self.rfile.read(length)
        request_id = self.headers.get("X-Request-ID", "")
        attempt = uuid.uuid4().hex[:12]
        tracing = (
            self.headers.get("X-Infra-Trace") == "1" and bool(request_id) and self.path != "/__infra_health"
        )

        def event(kind, **extra):
            if tracing:
                print(
                    json.dumps(
                        {
                            "infra_trace": 1,
                            "kind": kind,
                            "request_id": request_id,
                            "attempt_id": attempt,
                            "node": os.getenv("NODE_ID", "unknown"),
                            "time": time.time(),
                            **extra,
                        }
                    ),
                    flush=True,
                )

        event("app.received", path=self.path)
        if self.path == "/__infra_health":
            code, body = 200, {"health": "ok"}
        else:
            event("app.processing")
            time.sleep(int(os.getenv("DELAY_MS", "0")) / 1000)
            code = (
                int(os.getenv("ERROR_STATUS", "503"))
                if random.randrange(100) < int(os.getenv("ERROR_RATE", "0"))
                else 200
            )
            body = {
                "node": os.getenv("NODE_ID", "unknown"),
                "status": code,
                "request_id": self.headers.get("X-Request-ID"),
                "path": self.path,
                "time": time.time(),
            }
        content_type = "application/json; charset=utf-8"
        route = urlsplit(self.path).path
        extra_headers = []
        if route in ("/", "/page", "/details", "/search", "/submit"):
            content_type = "text/html; charset=utf-8"
            node = html.escape(os.getenv("NODE_ID", "unknown"))
            fields = (
                parse_qs(payload.decode("utf-8", errors="replace"))
                if self.command == "POST"
                else parse_qs(urlsplit(self.path).query)
            )
            value = html.escape(fields.get("message", fields.get("q", [""]))[0])
            title = (
                "提交结果"
                if route == "/submit"
                else "查询结果"
                if route == "/search"
                else "实例详情"
                if route == "/details"
                else "实验网站"
            )
            raw = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><link rel="stylesheet" href="/assets/site.css"><script defer src="/assets/site.js"></script></head><body><main><nav><a href="/">首页</a><a href="/details">实例详情</a><a href="/redirect">重定向测试</a></nav><h1>{title}</h1><dl><dt>当前实例</dt><dd>{node}</dd><dt>请求</dt><dd>{self.command} {html.escape(self.path)}</dd><dt>请求 ID</dt><dd>{html.escape(request_id)}</dd><dt>状态</dt><dd>{code}</dd></dl>{f"<p>收到内容：<strong>{value}</strong></p>" if value else ""}<section><h2>查询</h2><form action="/search" method="get"><input name="q" aria-label="查询内容" placeholder="输入查询内容"><button>查询</button></form><h2>提交表单</h2><form action="/submit" method="post"><input name="message" aria-label="表单内容" placeholder="输入提交内容"><button>提交</button></form><h2>异步请求</h2><button id="fetch">读取实例</button><pre id="response"></pre></section></main></body></html>""".encode()
            extra_headers.append(("Set-Cookie", "infra_demo=visited; Path=/; SameSite=Lax; HttpOnly"))
        elif route == "/assets/site.css":
            content_type = "text/css; charset=utf-8"
            raw = b"body{margin:0;background:#f2f4ef;color:#22382b;font:16px system-ui}main{max-width:800px;margin:32px auto;padding:24px}nav{display:flex;gap:24px}a{color:#2d6546}h1{font-size:30px}dl,section{background:white;padding:24px;border:1px solid #d7e2d8;border-radius:12px}dt{color:#657d6b;font-size:13px;margin-top:10px}dd{margin:6px 0;overflow-wrap:anywhere;font-family:monospace}form{display:flex;gap:10px}input{flex:1;min-width:0}input,button{padding:10px;border:1px solid #b4c8b8;border-radius:6px}button{cursor:pointer;background:#294f37;color:white}h2{font-size:18px}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        elif route == "/assets/site.js":
            content_type = "application/javascript; charset=utf-8"
            raw = b"document.querySelector('#fetch')?.addEventListener('click',async()=>{const r=await fetch('/api/instance');document.querySelector('#response').textContent=JSON.stringify(await r.json(),null,2)});"
        elif route == "/redirect":
            code = 302 if code == 200 else code
            extra_headers.append(("Location", "/details"))
            raw = b""
        else:
            body.update(
                method=self.command,
                cookie_present="infra_demo=visited" in self.headers.get("Cookie", ""),
            )
            raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-ID", request_id)
        self.send_header("Cache-Control", "no-store")
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        try:
            if not head:
                self.wfile.write(raw)
            self.wfile.flush()
            event("app.response_written", status=code)
        except (BrokenPipeError, ConnectionResetError):
            event("app.response_aborted", status=code)


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
