"""Shared validation for the controller and HTTP application (stdlib only)."""
import json
import re

DEFAULT = {"mode": "website", "status": 200, "format": "json", "body": "{}", "headers": [], "delay_ms": 0}
RESERVED = {"content-type", "content-length", "transfer-encoding", "connection", "keep-alive",
            "trailer", "te", "upgrade", "proxy-authenticate", "proxy-authorization", "server", "date"}


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError("响应配置字段不正确")
    data = {**DEFAULT, **value}
    if data["mode"] not in ("website", "custom") or data["format"] not in ("json", "text", "html"):
        raise ValueError("响应模式或正文类型不正确")
    for key, low, high in (("status", 200, 599), ("delay_ms", 0, 10000)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError(f"{key} 必须在 {low}–{high} 范围内")
    body = data["body"]
    if not isinstance(body, str) or len(body.encode("utf-8")) > 65536:
        raise ValueError("正文最大 64 KiB")
    if data["mode"] == "custom":
        if data["status"] in (204, 205, 304) and body:
            raise ValueError("204、205、304 不允许响应正文")
        if data["format"] == "json" and data["status"] not in (204, 205, 304):
            json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("JSON 不允许非有限数值")))
    headers = data["headers"]
    if not isinstance(headers, list) or len(headers) > 32:
        raise ValueError("响应头最多 32 个")
    size = 0
    for header in headers:
        if not isinstance(header, dict) or set(header) != {"name", "value"}:
            raise ValueError("响应头需要 name 和 value")
        name, content = header["name"], header["value"]
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("响应头名称无效")
        if name.lower() in RESERVED or name.lower().startswith(("x-infra-", "x-request-")):
            raise ValueError(f"{name} 由平台管理")
        if not isinstance(content, str) or any(ord(c) < 32 or ord(c) == 127 for c in content):
            raise ValueError("响应头不能包含控制字符")
        try:
            size += len(name.encode("ascii")) + len(content.encode("latin-1")) + 4
        except UnicodeError as exc:
            raise ValueError("响应头值须为 Latin-1 字符；中文请 URL 编码") from exc
    if size > 8192:
        raise ValueError("响应头总大小最大 8 KiB")
    return data
