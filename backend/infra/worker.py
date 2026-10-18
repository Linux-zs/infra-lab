import json
import os
import time
from pathlib import Path

from . import db
from .runtime import Runtime


def claim():
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        task = db.unpack(
            con.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        )
        if task:
            con.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (db.now(), task["id"]))
        return task


def execute(task, runtime):
    lab_id, task_id = task["lab_id"], task["id"]
    action = task["payload"]["action"]

    def emit(message):
        db.log(task_id, message)

    emit(f"开始 {task['kind']}，草稿版本 {action['revision']}")
    if task["kind"] == "apply":
        runtime.apply(lab_id, task["payload"]["topology"], task_id, emit)
        with db.connect() as con:
            con.execute(
                "UPDATE labs SET applied=?,applied_revision=? WHERE id=?",
                (json.dumps(task["payload"]["topology"]), action["revision"], lab_id),
            )
        return {"applied_revision": action["revision"]}
    if task["kind"] == "apply_response":
        from .response_runtime import apply_response
        response = task["payload"]["response"]
        return apply_response(runtime, lab_id, action["node_id"], response["version"], response["config"], emit)
    if task["kind"] == "reload":
        container = runtime.node(lab_id, action["node_id"])
        container.reload()
        if container.labels.get("infra.kind") != "nginx" or container.status != "running":
            raise RuntimeError("Nginx 未运行，无法重载")
        result = container.exec_run(["cat", "/etc/nginx/nginx.conf"])
        if result.exit_code:
            raise RuntimeError("读取运行配置失败")
        runtime.reload(container, result.output.decode(), emit)
        return {"node_id": action["node_id"], "reloaded": True}
    if task["kind"] == "delete_lab":
        with db.connect() as con:
            con.execute("UPDATE entries SET active=0,trace=0 WHERE lab_id=?", (lab_id,))
        containers = runtime.containers(lab_id)
        runtime.cleanup(lab_id, emit)
        with db.connect() as con:
            for container in containers:
                con.execute("DELETE FROM meta WHERE key=?", (f"node-ip:{container.id}",))
        db.erase_lab(lab_id)
        return {"deleted": True}
    if task["kind"] == "cleanup":
        runtime.cleanup(lab_id, emit)
        with db.connect() as con:
            con.execute("UPDATE labs SET applied=NULL,applied_revision=NULL WHERE id=?", (lab_id,))
        return {"cleaned": True}
    if task["kind"] in ("start", "stop"):
        container = runtime.node(lab_id, action["node_id"])
        if task["kind"] == "start":
            container.start()
            runtime.ready(container)
        else:
            container.stop(timeout=3)
        return {"node_id": action["node_id"], "action": task["kind"]}
    if task["kind"] in ("start_lab", "stop_lab"):
        return runtime.lifecycle(lab_id, task["kind"] == "start_lab", emit)
    if task["kind"] == "client_request":
        db.request_status(task_id, "running")
        result = runtime.client_request(lab_id, action, task_id, emit)
        with db.connect() as con:
            lab = db.unpack(con.execute("SELECT * FROM labs WHERE id=?", (lab_id,)).fetchone())
        result["applied_revision"] = lab["applied_revision"]
        db.request_status(task_id, "completed", result)
        return {"request_id": task_id}
    if task["kind"] == "redis_command":
        result = runtime.redis_command(lab_id, action)
        emit(f"{action['node_id']} {action['redis_op']}: {result['value']}")
        return result
    if task["kind"] == "load":
        # Read at execution time, not enqueue time: an earlier apply may have completed.
        with db.connect() as con:
            lab = db.unpack(con.execute("SELECT * FROM labs WHERE id=?", (lab_id,)).fetchone())
        result = runtime.load(lab_id, action, task_id, emit)
        result.update(
            {"applied_revision": lab["applied_revision"], "topology": lab["applied"], "parameters": action}
        )
        return result
    raise RuntimeError("不支持的任务")


def recover():
    with db.connect() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM tasks WHERE status='running'")]
    for task_id in ids:
        db.log(task_id, "Worker 上次执行中断；未自动重放。资源会重新探测，请核对后重新应用。")
        db.finish(task_id, "interrupted")
        db.request_event(
            task_id,
            "worker",
            "worker.interrupted",
            evidence="observed",
            data={"message": "Worker 中断；应用是否完成未知，未自动重发请求"},
        )
        db.request_status(task_id, "interrupted", {"error": "Worker 中断，未自动重发；应用状态未知"})


def heartbeat(available, error=None):
    with db.connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES('worker',?)",
            (json.dumps({"time": db.now(), "docker": available, "error": error}),),
        )


def main():
    db.init()
    # Singleton worker: kernel lock releases automatically on crash; DB serializes tasks.
    import fcntl

    lock = (Path(os.getenv("INFRA_DATA", "data")) / "worker.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    recover()
    while True:
        runtime = None
        try:
            runtime = Runtime()
            heartbeat(True)
            task = claim()
            if task:
                # Separate heartbeat thread keeps long operations distinguishable from a dead worker.
                import threading

                stop = threading.Event()

                def pulse(signal=stop):
                    while not signal.wait(5):
                        heartbeat(True)

                thread = threading.Thread(target=pulse, daemon=True)
                thread.start()
                try:
                    result = execute(task, runtime)
                    db.finish(task["id"], "failed" if result.get("partial_failure") else "succeeded", result)
                except Exception as exc:
                    db.log(task["id"], f"失败：{exc}。可能存在已完成步骤；保留现场。")
                    db.finish(task["id"], "failed", {"error": str(exc)})
                    db.request_event(task["id"], "worker", "worker.failed", data={"error": str(exc)})
                    db.request_status(task["id"], "failed", {"error": str(exc)})
                finally:
                    stop.set()
                    thread.join()
            with db.connect() as con:
                labs = [row[0] for row in con.execute("SELECT id FROM labs")]
            for lab_id in labs:
                try:
                    observation = runtime.observe(lab_id)
                except Exception as exc:
                    observation = {"available": False, "error": str(exc)}
                with db.connect() as con:
                    if observation["available"]:
                        con.execute(
                            "UPDATE labs SET observation=?,observed_at=? WHERE id=?",
                            (json.dumps(observation), db.now(), lab_id),
                        )
                    # Keep last good snapshot; its timestamp makes staleness visible.
            heartbeat(True)
        except Exception as exc:
            heartbeat(False, str(exc))
        finally:
            if runtime:
                runtime.close()
        time.sleep(2)


if __name__ == "__main__":
    main()
