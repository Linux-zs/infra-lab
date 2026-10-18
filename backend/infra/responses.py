import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db
from .response_config import DEFAULT, validate

router = APIRouter()


class SaveResponse(BaseModel):
    base_version: int = Field(ge=0)
    config: dict


def check_node(con, lab_id, node_id):
    row = con.execute("SELECT draft FROM labs WHERE id=?", (lab_id,)).fetchone()
    if not row:
        raise HTTPException(404, "实验不存在")
    if not any(n["id"] == node_id and n["kind"] == "app" for n in json.loads(row[0])["nodes"]):
        raise HTTPException(422, "请选择应用节点")


def saved(con, lab_id, node_id, version=None):
    query = "SELECT version,config FROM response_versions WHERE lab_id=? AND node_id=?"
    args = [lab_id, node_id]
    if version is not None:
        query += " AND version=?"
        args.append(version)
    row = con.execute(query + " ORDER BY version DESC LIMIT 1", args).fetchone()
    return {"version": row[0], "config": json.loads(row[1])} if row else None


@router.get("/api/labs/{lab_id}/nodes/{node_id}/response")
def get_response(lab_id: str, node_id: str):
    with db.connect() as con:
        check_node(con, lab_id, node_id)
        draft = saved(con, lab_id, node_id) or {"version": 0, "config": DEFAULT}
        state = con.execute("SELECT * FROM response_state WHERE lab_id=? AND node_id=?", (lab_id, node_id)).fetchone()
        history = [r[0] for r in con.execute("SELECT version FROM response_versions WHERE lab_id=? AND node_id=? ORDER BY version DESC LIMIT 30", (lab_id, node_id))]
        return {**draft, "state": dict(state) if state else None, "history": history}


@router.put("/api/labs/{lab_id}/nodes/{node_id}/response")
def save_response(lab_id: str, node_id: str, body: SaveResponse):
    try:
        config = validate(body.config)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        check_node(con, lab_id, node_id)
        previous = saved(con, lab_id, node_id)
        version = previous["version"] if previous else 0
        if body.base_version != version:
            raise HTTPException(409, "响应配置已改变，请重新读取后编辑")
        con.execute("INSERT INTO response_versions VALUES(?,?,?,?,?)", (lab_id, node_id, version + 1, json.dumps(config), db.now()))
    return get_response(lab_id, node_id)
