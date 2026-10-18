import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db
from .runtime import Runtime


def claim():
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        task = db.unpack(
            con.execute(
                "SELECT q.* FROM tasks q WHERE q.status='queued' AND NOT EXISTS ("
                "SELECT 1 FROM tasks running WHERE running.lab_id=q.lab_id AND running.status='running') "
                "ORDER BY q.created_at LIMIT 1"
            ).fetchone()
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
        runtime.cleanup(lab_id, emit)
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


def run_task(task):
    runtime = None
    stop = threading.Event()

    def pulse():
        while not stop.wait(5):
            heartbeat(True)

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        runtime = Runtime()
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
        if runtime:
            runtime.close()


def main():
    db.init()
    # Singleton worker: kernel lock releases automatically on crash; DB serializes tasks.
    import fcntl

    lock = (Path(os.getenv("INFRA_DATA", "data")) / "worker.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    recover()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="infra-task") as executor:
        observer = None
        history_migrated = False
        active = {}
        while True:
            try:
                if observer is None:
                    observer = Runtime()
                if not history_migrated:
                    observer.migrate_node_ip_history()
                    history_migrated = True
                for task_id, (_, future) in list(active.items()):
                    if future.done():
                        active.pop(task_id, None)
                while len(active) < 2:
                    task = claim()
                    if not task:
                        break
                    future = executor.submit(run_task, task)
                    active[task["id"]] = (task["lab_id"], future)
                busy_labs = {lab_id for lab_id, _ in active.values()}
                with db.connect() as con:
                    labs = [row[0] for row in con.execute("SELECT id FROM labs")]
                for lab_id in labs:
                    if lab_id in busy_labs:
                        continue
                    try:
                        observation = observer.observe(lab_id)
                    except Exception as exc:
                        observation = {"available": False, "error": str(exc)}
                    with db.connect() as con:
                        if observation["available"]:
                            con.execute(
                                "UPDATE labs SET observation=?,observed_at=? WHERE id=?",
                                (json.dumps(observation), db.now(), lab_id),
                            )
                heartbeat(True)
            except Exception as exc:
                heartbeat(False, str(exc))
                if observer:
                    observer.close()
                    observer = None
            time.sleep(1)


if __name__ == "__main__":
    main()
