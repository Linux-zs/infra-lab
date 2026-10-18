import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import db
from .entries import router as entries_router
from .models import Action, SaveDraft, normalize, render_config
from .responses import router as responses_router
from .responses import saved


@asynccontextmanager
async def lifespan(app):
    db.init()
    yield


app = FastAPI(title="Infra Lab", lifespan=lifespan)
app.include_router(entries_router)
app.include_router(responses_router)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=os.getenv("INFRA_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(","),
)


@app.middleware("http")
async def same_origin(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "拒绝跨站写入"}, status_code=403)
    return await call_next(request)


def get_lab(lab_id):
    with db.connect() as con:
        lab = db.unpack(con.execute("SELECT * FROM labs WHERE id=?", (lab_id,)).fetchone())
    if not lab:
        raise HTTPException(404, "实验不存在")
    return lab


@app.get("/api/health")
def health():
    with db.connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key='worker' ").fetchone()
    worker = json.loads(row[0]) if row else None
    return {"api": "online", "worker": worker, "worker_stale": not worker or db.now() - worker["time"] > 20}


class CreateLab(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@app.get("/api/labs")
def labs():
    with db.connect() as con:
        return [db.unpack(r) for r in con.execute("SELECT * FROM labs ORDER BY created_at")]


@app.post("/api/labs", status_code=201)
def create_lab(body: CreateLab):
    lab_id = db.uid()
    empty = json.dumps({"nodes": [], "edges": []})
    with db.connect() as con:
        con.execute(
            "INSERT INTO labs(id,name,draft,created_at) VALUES(?,?,?,?)", (lab_id, body.name, empty, db.now())
        )
        con.execute("INSERT INTO revisions VALUES(?,?,?,?)", (lab_id, 0, empty, db.now()))
    return get_lab(lab_id)


@app.get("/api/labs/{lab_id}")
def lab_detail(lab_id: str):
    return get_lab(lab_id)


@app.put("/api/labs/{lab_id}/draft")
def save_draft(lab_id: str, body: SaveDraft):
    data = normalize(body.topology)
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT revision FROM labs WHERE id=?", (lab_id,)).fetchone()
        if not row:
            raise HTTPException(404, "实验不存在")
        if db.deleting(con, lab_id):
            raise HTTPException(409, "实验正在删除")
        if row[0] != body.base_revision:
            raise HTTPException(409, "草稿已改变，请刷新后重试；本地编辑尚未覆盖")
        rev = row[0] + 1
        con.execute("UPDATE labs SET draft=?,revision=? WHERE id=?", (json.dumps(data), rev, lab_id))
        con.execute("INSERT INTO revisions VALUES(?,?,?,?)", (lab_id, rev, json.dumps(data), db.now()))
    return get_lab(lab_id)


@app.post("/api/config/preview")
def preview(body: SaveDraft, node_id: str):
    if not any(n.id == node_id and n.kind == "nginx" for n in body.topology.nodes):
        raise HTTPException(422, "请选择 Nginx 节点")
    return {"config": render_config(body.topology, node_id)}


@app.get("/api/labs/{lab_id}/revisions")
def revisions(lab_id: str):
    get_lab(lab_id)
    with db.connect() as con:
        return [
            db.unpack(r)
            for r in con.execute(
                "SELECT * FROM revisions WHERE lab_id=? ORDER BY revision DESC LIMIT 30", (lab_id,)
            )
        ]


@app.post("/api/labs/{lab_id}/tasks", status_code=202)
def submit(lab_id: str, body: Action):
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        lab = db.unpack(con.execute("SELECT * FROM labs WHERE id=?", (lab_id,)).fetchone())
        if not lab:
            raise HTTPException(404, "实验不存在")
        existing = db.unpack(
            con.execute(
                "SELECT * FROM tasks WHERE lab_id=? AND request_id=?", (lab_id, body.request_id)
            ).fetchone()
        )
        if existing:
            if Action.model_validate(existing["payload"]["action"]).model_dump() != body.model_dump():
                raise HTTPException(409, "同一请求标识不能用于不同操作")
            return existing
        receipt = con.execute(
            "SELECT action FROM task_receipts WHERE lab_id=? AND request_id=?", (lab_id, body.request_id)
        ).fetchone()
        if receipt:
            if json.loads(receipt[0]) != body.model_dump():
                raise HTTPException(409, "同一请求标识不能用于不同操作")
            raise HTTPException(410, "任务详情已归档；重复请求未执行，请使用新的请求标识")
        if db.deleting(con, lab_id):
            raise HTTPException(409, "实验正在删除")
        if body.kind == "delete_lab" and con.execute("SELECT 1 FROM tasks WHERE lab_id=? AND status IN ('queued','running')", (lab_id,)).fetchone():
            raise HTTPException(409, "请等待当前实验任务结束后再删除")
        if body.kind != "apply_response" and body.revision != lab["revision"]:
            raise HTTPException(409, "草稿版本已改变")
        topology = lab["draft"] if body.kind == "apply" else lab["applied"]
        if body.kind in ("start", "stop", "load", "client_request", "redis_command", "apply_response", "reload"):
            node = next((n for n in (topology or {}).get("nodes", []) if n["id"] == body.node_id), None)
            if not node:
                raise HTTPException(422, "请选择已部署的节点")
            if body.kind in ("load", "client_request") and node["kind"] != "nginx":
                raise HTTPException(422, "请求测试只允许指向本实验 Nginx")
            if body.kind == "redis_command" and node["kind"] != "redis":
                raise HTTPException(422, "请选择本实验已部署的 Redis 节点")
            if body.kind == "reload" and node["kind"] != "nginx":
                raise HTTPException(422, "重载仅支持 Nginx")
            if body.kind == "apply_response" and node["kind"] != "app":
                raise HTTPException(422, "响应配置仅支持应用节点")
        response = None
        if body.kind == "apply_response":
            response = saved(con, lab_id, body.node_id, body.response_version) if body.response_version else None
            if not response:
                raise HTTPException(422, "请选择已保存的响应配置版本")
        task_id = db.uid()
        payload = {
            "action": body.model_dump(),
            "response": response,
            "topology": topology,
            "applied_revision": lab["applied_revision"],
        }
        con.execute(
            "INSERT INTO tasks(id,lab_id,request_id,kind,status,payload,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, lab_id, body.request_id, body.kind, "queued", json.dumps(payload), db.now(), db.now()),
        )
        con.execute(
            "INSERT INTO task_receipts VALUES(?,?,?,?,?)",
            (lab_id, body.request_id, task_id, json.dumps(body.model_dump()), db.now()),
        )
        if body.kind == "client_request":
            con.execute(
                "INSERT INTO client_requests VALUES(?,?,?,?,?,?,NULL,?,?)",
                (task_id, lab_id, body.node_id, body.path, int(body.trace), "queued", db.now(), db.now()),
            )
        return db.unpack(con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


@app.get("/api/labs/{lab_id}/tasks")
def tasks(lab_id: str):
    get_lab(lab_id)
    with db.connect() as con:
        return [
            db.unpack(r)
            for r in con.execute(
                "SELECT * FROM tasks WHERE lab_id=? ORDER BY created_at DESC LIMIT 50", (lab_id,)
            )
        ]


@app.get("/api/labs/{lab_id}/nodes/{node_id}/logs")
def logs(lab_id: str, node_id: str):
    get_lab(lab_id)
    # Read only. Docker is never exposed to the browser.
    try:
        from .runtime import Runtime

        runtime = Runtime()
        try:
            return {"text": runtime.logs(lab_id, node_id)}
        finally:
            runtime.close()
    except Exception as exc:
        raise HTTPException(503, f"日志暂不可用：{exc}") from exc


@app.get("/api/labs/{lab_id}/events")
async def events(lab_id: str, request: Request):
    get_lab(lab_id)

    async def stream():
        while not await request.is_disconnected():
            try:
                data = {"lab": get_lab(lab_id), "tasks": tasks(lab_id), "health": health()}
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                yield 'event: deleted\ndata: {}\n\n'
                return
            yield f"event: snapshot\ndata: {json.dumps(data)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/labs/{lab_id}/requests")
def requests_list(lab_id: str):
    get_lab(lab_id)
    with db.connect() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT r.id,r.node_id,r.path,r.trace,r.status,r.created_at,r.updated_at,"
                "b.method,b.entry_id, CASE WHEN b.request_id IS NULL THEN 'single' ELSE 'browser' END AS source,"
                "json_extract(r.result,'$.status_code') AS status_code,json_extract(r.result,'$.latency_ms') AS latency_ms,json_extract(r.result,'$.error') AS error "
                "FROM client_requests r LEFT JOIN browser_requests b ON b.request_id=r.id "
                "WHERE r.lab_id=? ORDER BY r.created_at DESC LIMIT 100",
                (lab_id,),
            )
        ]


def request_events(lab_id, after=0):
    with db.connect() as con:
        return [
            db.unpack(r)
            for r in con.execute(
                "SELECT * FROM request_events WHERE lab_id=? AND seq>? ORDER BY seq LIMIT 500",
                (lab_id, after),
            )
        ]


@app.get("/api/labs/{lab_id}/requests/events")
async def trace_stream(lab_id: str, request: Request, after: int = 0):
    get_lab(lab_id)
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(422, "事件序号无效") from None

    async def stream():
        nonlocal cursor
        last_summary = 0
        while not await request.is_disconnected():
            for event in request_events(lab_id, cursor):
                cursor = event["seq"]
                yield f"id: {cursor}\nevent: trace\ndata: {json.dumps(event)}\n\n"
            if db.now() - last_summary > 1:
                yield f"event: requests\ndata: {json.dumps(requests_list(lab_id))}\n\n"
                last_summary = db.now()
            await asyncio.sleep(0.25)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/labs/{lab_id}/requests/{request_id}")
def request_detail(lab_id: str, request_id: str):
    with db.connect() as con:
        value = db.unpack(
            con.execute(
                "SELECT * FROM client_requests WHERE lab_id=? AND id=?", (lab_id, request_id)
            ).fetchone()
        )
        if not value:
            raise HTTPException(404, "请求不存在或已超出最近 100 次保留范围")
        browser = con.execute("SELECT * FROM browser_requests WHERE request_id=?", (request_id,)).fetchone()
        value.update(
            source="browser" if browser else "single", method=browser["method"] if browser else "GET"
        )
        value["events"] = [
            db.unpack(r)
            for r in con.execute(
                "SELECT * FROM request_events WHERE request_id=? ORDER BY seq", (request_id,)
            )
        ]
        return value


static = Path(os.getenv("INFRA_WEB", "../frontend/dist"))
if static.is_dir():
    app.mount("/", StaticFiles(directory=static, html=True), name="web")
