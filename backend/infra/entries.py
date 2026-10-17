import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db

router = APIRouter(prefix="/api/labs/{lab_id}/entries")


class EntryInput(BaseModel):
    node_id: str
    trace: bool = False


class TraceInput(BaseModel):
    trace: bool


def describe(row):
    value = dict(row)
    value["url"] = f"http://{value['hostname']}:8089/"
    return value


@router.get("")
def list_entries(lab_id: str):
    with db.connect() as con:
        return [
            describe(r) for r in con.execute("SELECT * FROM entries WHERE lab_id=? AND active=1", (lab_id,))
        ]


@router.post("")
def create_entry(lab_id: str, body: EntryInput):
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        lab = db.unpack(con.execute("SELECT * FROM labs WHERE id=?", (lab_id,)).fetchone())
        if not lab or not any(
            n["id"] == body.node_id and n["kind"] == "nginx" for n in (lab["applied"] or {}).get("nodes", [])
        ):
            raise HTTPException(422, "请选择已部署的 Nginx")
        row = con.execute(
            "SELECT * FROM entries WHERE lab_id=? AND node_id=? AND active=1", (lab_id, body.node_id)
        ).fetchone()
        if not row:
            entry_id = secrets.token_hex(16)
            con.execute(
                "INSERT INTO entries VALUES(?,?,?,?,1,?,?)",
                (entry_id, lab_id, body.node_id, f"{entry_id}.localhost", int(body.trace), db.now()),
            )
            row = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return describe(row)


@router.patch("/{entry_id}")
def set_trace(lab_id: str, entry_id: str, body: TraceInput):
    with db.connect() as con:
        result = con.execute(
            "UPDATE entries SET trace=? WHERE id=? AND lab_id=? AND active=1",
            (int(body.trace), entry_id, lab_id),
        )
        if not result.rowcount:
            raise HTTPException(404, "入口已撤销或不存在")
    return {"trace": body.trace}


@router.delete("/{entry_id}")
def revoke(lab_id: str, entry_id: str):
    with db.connect() as con:
        con.execute("UPDATE entries SET active=0,trace=0 WHERE id=? AND lab_id=?", (entry_id, lab_id))
    return {"revoked": True}
