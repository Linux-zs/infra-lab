from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, model_validator


class Position(BaseModel):
    x: float = Field(ge=-10000, le=10000, allow_inf_nan=False)
    y: float = Field(ge=-10000, le=10000, allow_inf_nan=False)


class Node(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    kind: Literal["nginx", "app"]
    position: Position
    memory_mb: int = Field(default=128, ge=64, le=512)
    cpu: float = Field(default=0.5, ge=0.1, le=1)
    delay_ms: int = Field(default=0, ge=0, le=10000)
    error_rate: int = Field(default=0, ge=0, le=100)
    error_status: Literal[500, 502, 503, 504] = 503
    mode: Literal["generated", "manual"] = "generated"
    config: str = Field(default="", max_length=65536)


class Edge(BaseModel):
    source: str
    target: str
    weight: int = Field(default=1, ge=1, le=100)


class Topology(BaseModel):
    nodes: list[Node] = Field(default_factory=list, max_length=10)
    edges: list[Edge] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def valid_graph(self):
        nodes = {n.id: n for n in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("节点 ID 重复")
        seen = set()
        for edge in self.edges:
            if edge.source not in nodes or edge.target not in nodes:
                raise ValueError("连线端点不存在")
            if nodes[edge.source].kind != "nginx" or nodes[edge.target].kind != "app":
                raise ValueError("仅支持 Nginx → HTTP 应用")
            if (edge.source, edge.target) in seen:
                raise ValueError("连线重复")
            seen.add((edge.source, edge.target))
        return self


class SaveDraft(BaseModel):
    base_revision: int
    topology: Topology


class Action(BaseModel):
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")
    kind: Literal["apply", "start", "stop", "cleanup", "load", "start_lab", "stop_lab", "client_request"]
    revision: int
    node_id: str | None = None
    count: int = Field(default=100, ge=1, le=1000)
    concurrency: int = Field(default=5, ge=1, le=20)
    duration: int = Field(default=30, ge=1, le=60)
    path: str = Field(default="/page", max_length=2048)
    trace: bool = False

    @model_validator(mode="after")
    def relative_path(self):
        parsed = urlsplit(self.path)
        decoded = unquote(self.path)
        if (
            not self.path.startswith("/")
            or self.path.startswith("//")
            or parsed.netloc
            or parsed.scheme
            or parsed.fragment
            or decoded.startswith("//")
            or "\\" in decoded
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
        ):
            raise ValueError("只允许当前入口的相对路径及查询参数，如 /page?name=test")
        return self


def render_config(topology: Topology, node_id: str) -> str:
    targets = [e for e in topology.edges if e.source == node_id]
    upstream = "\n".join(
        f"    server {e.target}:8080 weight={e.weight} max_fails=2 fail_timeout=5s;" for e in targets
    )
    block = f"upstream backends {{\n{upstream}\n  }}" if targets else ""
    route = (
        """proxy_pass http://backends;
      proxy_connect_timeout 1s;
      proxy_read_timeout 2s;
      proxy_next_upstream error timeout http_502 http_503 http_504;
      proxy_next_upstream_tries 2;
      proxy_set_header X-Request-ID $http_x_request_id;"""
        if targets
        else 'return 503 "No upstream configured\\n";'
    )
    return f"""worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr notice;
events {{ worker_connections 256; }}
http {{
  client_body_temp_path /tmp/client_body;
  proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi;
  uwsgi_temp_path /tmp/uwsgi;
  scgi_temp_path /tmp/scgi;
  log_format lab escape=json '{{"request_id":"$http_x_request_id","status":$status,"upstream":"$upstream_addr","upstream_status":"$upstream_status","time":"$request_time"}}';
  access_log /dev/stdout lab;
  {block}
  server {{
    listen 8080;
    location = /__infra_health {{ access_log off; return 200 "ok"; }}
    location / {{
      {route}
    }}
  }}
}}
"""


def normalize(topology: Topology) -> dict:
    data = topology.model_dump()
    for node in data["nodes"]:
        if node["kind"] == "nginx" and node["mode"] == "generated":
            node["config"] = render_config(topology, node["id"])
    return data
