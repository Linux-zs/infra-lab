import copy
import hashlib
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .models import Action, Topology, normalize
from .response_config import validate
from .responses import saved
from .scenario_catalog import CATALOG, public_definition

router = APIRouter()


class CreateScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")


class RunStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: int = Field(ge=0)
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")
    count: int = Field(default=120, ge=1, le=1000)
    concurrency: int = Field(default=1, ge=1, le=20)
    duration: int = Field(default=30, ge=1, le=60)
    trace: bool = False
    value: str = Field(default="场景复制验证", max_length=4096)


def lab_with_scenario(con, lab_id):
    value = db.unpack(
        con.execute(
            "SELECT labs.*,s.template_id AS scenario_id FROM labs LEFT JOIN lab_scenarios s ON s.lab_id=labs.id WHERE labs.id=?",
            (lab_id,),
        ).fetchone()
    )
    if not value:
        raise HTTPException(404, "实验不存在")
    return value


def definition_for(con, lab_id):
    row = con.execute("SELECT definition FROM lab_scenarios WHERE lab_id=?", (lab_id,)).fetchone()
    if not row:
        raise HTTPException(404, "此实验没有场景步骤")
    return json.loads(row[0])


@router.get("/api/scenarios")
def templates():
    return [public_definition(value) for value in CATALOG.values()]


@router.post("/api/scenarios/{template_id}/labs", status_code=201)
def create(template_id: str, body: CreateScenario):
    definition = CATALOG.get(template_id)
    if not definition:
        raise HTTPException(404, "场景不存在")
    signature = hashlib.sha256(
        json.dumps({"template_id": template_id, **body.model_dump()}, sort_keys=True).encode()
    ).hexdigest()
    name = body.name.strip() if body.name is not None else definition["title"]
    if not name:
        raise HTTPException(422, "实验名称不能为空")
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        receipt = con.execute(
            "SELECT * FROM scenario_creations WHERE request_id=?", (body.request_id,)
        ).fetchone()
        if receipt:
            if receipt["signature"] != signature:
                raise HTTPException(409, "同一请求标识不能用于不同场景")
            if receipt["lab_id"] is None:
                raise HTTPException(410, "该次创建的实验已删除；未重复创建")
            return lab_with_scenario(con, receipt["lab_id"])
        lab_id = db.uid()
        topology = json.dumps(definition["topology"])
        con.execute(
            "INSERT INTO labs(id,name,revision,draft,created_at) VALUES(?,?,1,?,?)",
            (lab_id, name, topology, db.now()),
        )
        con.execute("INSERT INTO revisions VALUES(?,?,?,?)", (lab_id, 1, topology, db.now()))
        con.execute("INSERT INTO lab_scenarios VALUES(?,?,?)", (lab_id, template_id, json.dumps(definition)))
        con.execute("INSERT INTO scenario_creations VALUES(?,?,?)", (body.request_id, signature, lab_id))
        return lab_with_scenario(con, lab_id)


@router.get("/api/labs/{lab_id}/scenario")
def state(lab_id: str):
    with db.connect() as con:
        lab_with_scenario(con, lab_id)
        definition = definition_for(con, lab_id)
        runs = [
            db.unpack(r)
            for r in con.execute(
                "SELECT * FROM scenario_runs WHERE lab_id=? ORDER BY created_at DESC LIMIT 100", (lab_id,)
            )
        ]
    return {"template": public_definition(definition), "runs": runs}


def semantic(topology):
    value = copy.deepcopy(topology)
    for node in value.get("nodes", []):
        node.pop("position", None)
    return value


def plan_step(con, lab, definition, entry):
    recipe = copy.deepcopy(entry["recipe"])
    applied = lab["applied"]
    if applied is None:
        raise HTTPException(409, "请先应用拓扑，场景创建只保存草稿")
    expected = {(n["id"], n["kind"]) for n in definition["topology"]["nodes"]}
    if {(n["id"], n["kind"]) for n in applied["nodes"]} != expected:
        raise HTTPException(409, "已部署节点与场景不兼容，请手动操作或新建场景实验")
    plan = {**recipe, "responses": {}, "apply_topology": None}
    if "weights" in recipe or "nginx" in recipe:
        if semantic(lab["draft"]) != semantic(applied):
            raise HTTPException(409, "有尚未应用的拓扑修改，请先处理草稿")
        changed = copy.deepcopy(lab["draft"])
        gateway = next(n for n in changed["nodes"] if n["id"] == "gateway")
        if "weights" in recipe:
            if gateway["mode"] != "generated":
                raise HTTPException(409, "gateway 使用手动配置，请先切回拓扑生成后执行权重步骤")
            expected_edges = {(e["source"], e["target"]) for e in definition["topology"]["edges"]}
            if {(e["source"], e["target"]) for e in changed["edges"]} != expected_edges:
                raise HTTPException(409, "连接结构已改变，不能直接执行此权重步骤")
            weights = dict(zip(("app-a", "app-b", "app-c"), recipe["weights"], strict=True))
            for edge in changed["edges"]:
                edge["weight"] = weights[edge["target"]]
        else:
            known = {definition["topology"]["nodes"][0]["config"]}
            known.update(s["recipe"]["nginx"] for s in definition["steps"] if "nginx" in s["recipe"])
            if gateway["config"] not in known:
                raise HTTPException(409, "Nginx 配置已手动改变，此步骤不会覆盖它")
            gateway.update(mode="manual", config=recipe["nginx"])
        changed = normalize(Topology.model_validate(changed))
        if changed != lab["draft"]:
            lab["revision"] += 1
            lab["draft"] = changed
            con.execute(
                "UPDATE labs SET revision=?,draft=? WHERE id=?",
                (lab["revision"], json.dumps(changed), lab["id"]),
            )
            con.execute(
                "INSERT INTO revisions VALUES(?,?,?,?)",
                (lab["id"], lab["revision"], json.dumps(changed), db.now()),
            )
        plan["apply_topology"] = changed
    for node_id, config in recipe.get("responses", {}).items():
        config = validate(config)
        latest = saved(con, lab["id"], node_id)
        row = con.execute(
            "SELECT applied_version FROM response_state WHERE lab_id=? AND node_id=?", (lab["id"], node_id)
        ).fetchone()
        applied_version = row[0] if row else 0
        if latest and latest["version"] != applied_version and latest["config"] != config:
            raise HTTPException(409, f"{node_id} 有尚未应用的响应配置，请先处理它")
        if latest and latest["config"] == config:
            candidate = latest
        else:
            version = latest["version"] + 1 if latest else 1
            con.execute(
                "INSERT INTO response_versions VALUES(?,?,?,?,?)",
                (lab["id"], node_id, version, json.dumps(config), db.now()),
            )
            candidate = {"version": version, "config": config}
        plan["responses"][node_id] = candidate
    plan["revision"] = lab["revision"]
    return plan


@router.post("/api/labs/{lab_id}/scenario/steps/{step_id}/run", status_code=202)
def run(lab_id: str, step_id: str, body: RunStep):
    action = Action(
        kind="scenario_step",
        step_id=step_id,
        revision=body.base_revision,
        request_id=body.request_id,
        count=body.count,
        concurrency=body.concurrency,
        duration=body.duration,
        trace=body.trace,
        value=body.value,
    ).model_dump()
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        lab = lab_with_scenario(con, lab_id)
        existing = db.unpack(
            con.execute(
                "SELECT * FROM tasks WHERE lab_id=? AND request_id=?", (lab_id, body.request_id)
            ).fetchone()
        )
        if existing:
            if Action.model_validate(existing["payload"]["action"]).model_dump() != action:
                raise HTTPException(409, "同一请求标识不能用于不同步骤或参数")
            return existing
        receipt = con.execute(
            "SELECT action FROM task_receipts WHERE lab_id=? AND request_id=?", (lab_id, body.request_id)
        ).fetchone()
        if receipt:
            if Action.model_validate(json.loads(receipt[0])).model_dump() != action:
                raise HTTPException(409, "同一请求标识不能用于不同步骤或参数")
            raise HTTPException(410, "该次执行已归档；未重复执行")
        if con.execute(
            "SELECT 1 FROM tasks WHERE lab_id=? AND status IN ('queued','running')", (lab_id,)
        ).fetchone():
            raise HTTPException(409, "请等待当前实验任务结束")
        if lab["revision"] != body.base_revision:
            raise HTTPException(409, "草稿版本已改变，请刷新后再执行步骤")
        definition = definition_for(con, lab_id)
        entry = next((s for s in definition["steps"] if s["id"] == step_id), None)
        if not entry:
            raise HTTPException(404, "步骤不存在")
        plan = plan_step(con, lab, definition, entry)
        task_id, now = db.uid(), db.now()
        payload = {
            "action": action,
            "scenario_plan": plan,
            "template_id": definition["id"],
            "template_version": definition["version"],
        }
        con.execute(
            "INSERT INTO tasks(id,lab_id,request_id,kind,status,payload,created_at,updated_at) VALUES(?,?,?,'scenario_step','queued',?,?,?)",
            (task_id, lab_id, body.request_id, json.dumps(payload), now, now),
        )
        con.execute(
            "INSERT INTO task_receipts VALUES(?,?,?,?,?)",
            (lab_id, body.request_id, task_id, json.dumps(action), now),
        )
        snapshot = {
            "template_id": definition["id"],
            "template_version": definition["version"],
            "submitted_revision": body.base_revision,
            "target_revision": plan["revision"],
            "before": None,
            "after": None,
        }
        con.execute(
            "INSERT INTO scenario_runs VALUES(?,?,?,'queued',?,?,NULL,?,NULL)",
            (task_id, lab_id, step_id, json.dumps(action), json.dumps(snapshot), now),
        )
        if plan["measure"] == "request":
            con.execute(
                "INSERT INTO client_requests VALUES(?,?,?,?,?,'queued',NULL,?,?)",
                (task_id, lab_id, "gateway", "/page", int(body.trace), now, now),
            )
        return db.unpack(con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
