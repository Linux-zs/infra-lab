"""Versioned recipes. Every effect below is executed against real services."""

import copy

from .models import Topology, normalize


def node(name, kind, x, y):
    return {"id": name, "kind": kind, "position": {"x": x, "y": y}, "memory_mb": 64, "cpu": 0.25}


def http_topology(apps):
    return normalize(
        Topology.model_validate(
            {
                "nodes": [node("gateway", "nginx", 40, 150)]
                + [node(name, "app", 410, i * 180) for i, name in enumerate(apps)],
                "edges": [{"source": "gateway", "target": name, "weight": 1} for name in apps],
            }
        )
    )


def delay_config(timeout="2s", limited=False):
    config = http_topology(["app-a"])["nodes"][0]["config"]
    config = config.replace("proxy_read_timeout 2s", f"proxy_read_timeout {timeout}")
    if limited:
        config = config.replace("location / {", "location / {\n      limit_rate 1k;")
    return config


def response(status=200, delay=0, body="场景响应", mode="custom"):
    return {"mode": mode, "status": status, "format": "text", "body": body, "delay_ms": delay, "headers": []}


def step(step_id, title, effect, observe, **recipe):
    return {"id": step_id, "title": title, "effect": effect, "observe": observe, "recipe": recipe}


def catalog():
    weights = http_topology(["app-a", "app-b", "app-c"])
    retry = http_topology(["app-a", "app-b"])
    retry["nodes"][0]["mode"] = "manual"
    retry["nodes"][0]["config"] = retry["nodes"][0]["config"].replace(
        "server app-b:8080 weight=1 max_fails=2 fail_timeout=5s;", "server app-b:8080 backup;"
    )
    delay = http_topology(["app-a"])
    delay["nodes"][0].update(mode="manual", config=delay_config())
    redis = normalize(
        Topology.model_validate(
            {
                "nodes": [
                    node("redis-primary", "redis", 40, 150),
                    node("redis-replica-1", "redis", 410, 20),
                    node("redis-replica-2", "redis", 410, 260),
                ],
                "edges": [
                    {"source": "redis-primary", "target": name, "weight": 1}
                    for name in ("redis-replica-1", "redis-replica-2")
                ],
            }
        )
    )
    items = [
        {
            "id": "nginx-weights",
            "version": 1,
            "title": "Nginx 权重分流",
            "topology": weights,
            "metrics": ["请求分布", "错误率", "P95 耗时"],
            "steps": [
                step(
                    "baseline",
                    "记录当前分布",
                    "按当前运行配置发送请求。",
                    "记录实际请求数和各实例占比，作为对比基线。",
                    measure="load",
                ),
                step(
                    "weighted",
                    "设置 3:1:1 并测试",
                    "将 gateway 到 app-a、app-b、app-c 的权重改为 3、1、1，应用后发送请求。",
                    "比较修改前后的真实分布；有限请求量不保证精确比例。",
                    measure="load",
                    weights=[3, 1, 1],
                ),
                step(
                    "equal",
                    "恢复等权并测试",
                    "将三个连接权重恢复为 1，应用后发送请求。",
                    "比较配置恢复后的请求分布。",
                    measure="load",
                    weights=[1, 1, 1],
                ),
            ],
        },
        {
            "id": "nginx-retry",
            "version": 1,
            "title": "故障与重试",
            "topology": retry,
            "metrics": ["响应实例", "upstream 尝试顺序", "响应状态"],
            "steps": [
                step(
                    "baseline",
                    "发送正常请求",
                    "向 gateway 发送一次 GET /page。",
                    "开启链路后核对实际命中的实例。",
                    measure="request",
                ),
                step(
                    "error",
                    "设置 503 并请求",
                    "独立发布 app-a 的固定 503 响应，再发送请求。",
                    "从 Nginx 完成日志确认是否重试到备用 app-b。",
                    measure="request",
                    responses={"app-a": response(503, body="主应用返回 503")},
                ),
                step(
                    "stop",
                    "停止主应用并请求",
                    "停止 app-a，保留容器和配置，再发送请求。",
                    "查看连接失败后的实际尝试顺序及最终响应。",
                    measure="request",
                    lifecycle={"app-a": "stop"},
                ),
                step(
                    "recover",
                    "恢复主应用并请求",
                    "启动 app-a，恢复默认网站，重载当前 Nginx 配置以清除被动故障状态，再发送请求。",
                    "核对主应用是否重新接收请求。",
                    measure="request",
                    lifecycle={"app-a": "start"},
                    responses={"app-a": response(mode="website")},
                    reload=True,
                ),
            ],
        },
        {
            "id": "nginx-timeout",
            "version": 1,
            "title": "延迟与超时",
            "topology": delay,
            "metrics": ["客户端耗时", "Nginx 状态", "应用事件时间"],
            "steps": [
                step(
                    "baseline",
                    "记录正常响应",
                    "向 gateway 发送一次 GET /page。",
                    "记录正常耗时，作为后续对比基线。",
                    measure="request",
                ),
                step(
                    "slow",
                    "设置慢响应并请求",
                    "将 app-a 延迟设为 8 秒，Nginx 读取超时设为 2 秒，再发送请求。",
                    "区分 Nginx 返回 504 与应用之后的写出或中止事件。",
                    measure="request",
                    responses={"app-a": response(delay=8000, body="8 秒慢响应")},
                    nginx=delay_config(),
                ),
                step(
                    "wait",
                    "延长等待并请求",
                    "保持应用延迟 8 秒，将 Nginx 读取超时改为 12 秒，再发送请求。",
                    "比较客户端耗时、状态和配置版本。",
                    measure="request",
                    responses={"app-a": response(delay=8000, body="8 秒慢响应")},
                    nginx=delay_config("12s"),
                ),
                step(
                    "client-timeout",
                    "限速触发客户端时限",
                    "发布 16 KiB 正文，应用处理 1 秒，Nginx 以每秒 1 KiB 发送；单次请求总时限仍为 10 秒。",
                    "核对客户端超时、部分正文及应用处理结束时间，避免把接收超时当作应用取消。",
                    measure="request",
                    responses={"app-a": response(delay=1000, body="x" * 16384)},
                    nginx=delay_config("12s", True),
                ),
                step(
                    "recover",
                    "恢复正常响应",
                    "恢复默认网站和 Nginx 的 2 秒读取超时，再发送请求。",
                    "核对恢复后的实际状态和耗时。",
                    measure="request",
                    responses={"app-a": response(mode="website")},
                    nginx=delay_config(),
                ),
            ],
        },
        {
            "id": "redis-replication",
            "version": 1,
            "title": "Redis 主从复制",
            "topology": redis,
            "metrics": ["读写结果", "实际复制角色", "复制连接状态"],
            "steps": [
                step(
                    "replicate",
                    "写入并读取主从",
                    "在本实验专属键写入指定内容，读取主节点和两个从节点。",
                    "查看真实读取值；同步未完成时保留实际观测。",
                    measure="redis",
                    redis="replicate",
                ),
                step(
                    "readonly",
                    "验证从节点只读",
                    "尝试在 redis-replica-1 写入本实验专属键。",
                    "记录 Redis 实际返回的 READONLY；其他错误仍按失败报告。",
                    measure="redis",
                    redis="readonly",
                ),
                step(
                    "stop",
                    "停止主节点并读取",
                    "停止 redis-primary，读取两个从节点并采集实际角色。",
                    "观察数据保留、复制断连及是否发生角色变化。",
                    measure="redis",
                    redis="read",
                    lifecycle={"redis-primary": "stop"},
                ),
                step(
                    "recover",
                    "启动主节点并读取",
                    "启动已有 redis-primary，采集复制状态并重新读取主从。",
                    "核对数据与连接恢复情况；此场景没有自动切主服务。",
                    measure="redis",
                    redis="read-all",
                    lifecycle={"redis-primary": "start"},
                ),
            ],
        },
    ]
    return {item["id"]: item for item in items}


CATALOG = catalog()


def public_definition(definition):
    value = copy.deepcopy(definition)
    for entry in value["steps"]:
        entry["measure"] = entry["recipe"]["measure"]
        del entry["recipe"]
    value["node_count"] = len(value["topology"]["nodes"])
    value["memory_mb"] = sum(n["memory_mb"] for n in value["topology"]["nodes"])
    del value["topology"]
    return value
