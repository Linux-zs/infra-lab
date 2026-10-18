import hashlib
import json

from . import db
from .response_config import validate

CAPABILITY = "client-page-trace-v3"


def envelope(version, config):
    config = validate(config)
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return {"version": version, "config": config, "digest": digest}


def probe(container):
    result = container.exec_run(["python", "-c", "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/__infra_health',timeout=2).read().decode())"])
    if result.exit_code:
        raise RuntimeError(result.output.decode(errors="replace"))
    info = json.loads(result.output)
    if info.get("response_error"):
        raise RuntimeError(info["response_error"])
    return info


def reconcile(lab_id, node_id, container):
    try:
        info = probe(container)
        version = info["response_version"]
        digest = info.get("response_digest")
        with db.connect() as con:
            row = con.execute("SELECT config FROM response_versions WHERE lab_id=? AND node_id=? AND version=?", (lab_id, node_id, version)).fetchone()
            if version and (not row or envelope(version, json.loads(row[0]))["digest"] != digest):
                raise RuntimeError("运行配置与已保存版本不一致")
            con.execute("INSERT INTO response_state VALUES(?,?,?,?,?,?) ON CONFLICT(lab_id,node_id) DO UPDATE SET applied_version=excluded.applied_version,observed_version=excluded.observed_version,observed_at=excluded.observed_at,error=NULL", (lab_id, node_id, version, version, db.now(), None))
        config = json.loads(row[0]) if row else {"mode": "website"}
        return {"version": version, "observed_at": db.now(), "available": True,
                "mode": config["mode"], "status": config.get("status"), "delay_ms": config.get("delay_ms")}
    except Exception as exc:
        with db.connect() as con:
            con.execute("INSERT INTO response_state VALUES(?,?,0,NULL,?,?) ON CONFLICT(lab_id,node_id) DO UPDATE SET observed_version=NULL,observed_at=excluded.observed_at,error=excluded.error", (lab_id, node_id, db.now(), str(exc)))
        return {"available": False, "error": str(exc), "observed_at": db.now()}


def apply_response(runtime, lab_id, node_id, version, config, emit):
    from .runtime import archive

    container = runtime.node(lab_id, node_id)
    container.reload()
    if container.status != "running":
        raise RuntimeError("应用未运行，请先启动；已保存配置保留")
    if container.labels.get("infra.kind") != "app" or container.labels.get("infra.capabilities") != CAPABILITY:
        raise RuntimeError("应用版本不支持自定义响应，请主动应用拓扑升级")
    candidate = envelope(version, config)
    old = container.exec_run(["cat", "/tmp/infra-response.json"])
    container.put_archive("/tmp", archive("infra-response.next", json.dumps(candidate), uid=65534))
    publish = "import json,os; from response_config import validate; p='/tmp/infra-response.next'; validate(json.load(open(p))['config']); os.replace(p,'/tmp/infra-response.json')"
    try:
        result = container.exec_run(["python", "-c", publish])
        if result.exit_code:
            raise RuntimeError(result.output.decode(errors="replace"))
        info = probe(container)
        if info["response_version"] != version or info["response_digest"] != candidate["digest"]:
            raise RuntimeError("未确认运行版本")
    except Exception:
        if old.exit_code == 0:
            container.put_archive("/tmp", archive("infra-response.next", old.output.decode(), uid=65534))
            container.exec_run(["python", "-c", "import os; os.replace('/tmp/infra-response.next','/tmp/infra-response.json')"])
        else:
            container.exec_run(["python", "-c", "from pathlib import Path; Path('/tmp/infra-response.json').unlink(missing_ok=True)"])
        reconcile(lab_id, node_id, container)
        raise
    state = reconcile(lab_id, node_id, container)
    if not state["available"]:
        raise RuntimeError(state["error"])
    emit(f"{node_id} 响应配置 v{version} 已确认生效")
    return {"node_id": node_id, "response_version": version}
