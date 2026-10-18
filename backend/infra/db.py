import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def now():
    return time.time()


def uid():
    return uuid.uuid4().hex[:16]


@contextmanager
def connect():
    root = Path(os.environ.get("INFRA_DATA", "data"))
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "infra.db", timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init():
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS response_versions (
          lab_id TEXT NOT NULL,node_id TEXT NOT NULL,version INTEGER NOT NULL,
          config TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(lab_id,node_id,version)
        );
        CREATE TABLE IF NOT EXISTS response_state (
          lab_id TEXT NOT NULL,node_id TEXT NOT NULL,applied_version INTEGER NOT NULL DEFAULT 0,
          observed_version INTEGER,observed_at REAL,error TEXT,PRIMARY KEY(lab_id,node_id)
        );
        CREATE TABLE IF NOT EXISTS labs (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
          draft TEXT NOT NULL, applied TEXT, applied_revision INTEGER,
          observation TEXT, observed_at REAL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revisions (
          lab_id TEXT NOT NULL, revision INTEGER NOT NULL, topology TEXT NOT NULL,
          created_at REAL NOT NULL, PRIMARY KEY(lab_id,revision)
        );
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, lab_id TEXT NOT NULL, request_id TEXT NOT NULL,
          kind TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
          logs TEXT NOT NULL DEFAULT '[]', result TEXT, created_at REAL NOT NULL,
          updated_at REAL NOT NULL, UNIQUE(lab_id,request_id)
        );
        CREATE TABLE IF NOT EXISTS task_receipts (
          lab_id TEXT NOT NULL, request_id TEXT NOT NULL, task_id TEXT NOT NULL,
          action TEXT NOT NULL, created_at REAL NOT NULL,
          PRIMARY KEY(lab_id,request_id)
        );
        CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status,created_at);
        CREATE INDEX IF NOT EXISTS tasks_lab_created ON tasks(lab_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS tasks_lab_status ON tasks(lab_id,status);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS client_requests (
          id TEXT PRIMARY KEY, lab_id TEXT NOT NULL, node_id TEXT NOT NULL,
          path TEXT NOT NULL, trace INTEGER NOT NULL, status TEXT NOT NULL,
          result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS request_events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
          lab_id TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
          node_id TEXT, evidence TEXT NOT NULL, time REAL NOT NULL,
          received_at REAL NOT NULL, data TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_lab_seq ON request_events(lab_id,seq);
        CREATE INDEX IF NOT EXISTS events_request ON request_events(request_id,seq);
        CREATE TABLE IF NOT EXISTS entries (
          id TEXT PRIMARY KEY, lab_id TEXT NOT NULL, node_id TEXT NOT NULL,
          hostname TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          trace INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS active_entry ON entries(lab_id,node_id) WHERE active=1;
        CREATE TABLE IF NOT EXISTS browser_requests (
          request_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, method TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lab_scenarios (
          lab_id TEXT PRIMARY KEY, template_id TEXT NOT NULL, definition TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scenario_creations (
          request_id TEXT PRIMARY KEY, signature TEXT NOT NULL, lab_id TEXT
        );
        CREATE TABLE IF NOT EXISTS scenario_runs (
          task_id TEXT PRIMARY KEY, lab_id TEXT NOT NULL, step_id TEXT NOT NULL,
          status TEXT NOT NULL, parameters TEXT NOT NULL, snapshot TEXT NOT NULL,
          result TEXT, created_at REAL NOT NULL, finished_at REAL
        );
        CREATE INDEX IF NOT EXISTS scenario_runs_lab ON scenario_runs(lab_id,created_at DESC);
        """)
        # Preserve request deduplication after bulky task details are pruned.
        for row in con.execute("SELECT lab_id,request_id,id,payload,created_at FROM tasks"):
            action = json.loads(row["payload"]).get("action", {})
            con.execute(
                "INSERT OR IGNORE INTO task_receipts VALUES(?,?,?,?,?)",
                (row["lab_id"], row["request_id"], row["id"], json.dumps(action), row["created_at"]),
            )


def unpack(row):
    if row is None:
        return None
    data = dict(row)
    for field in ("draft", "applied", "observation", "payload", "logs", "result", "topology", "data", "definition", "parameters", "snapshot"):
        if field in data and data[field] is not None:
            data[field] = json.loads(data[field])
    return data


def log(task_id, message):
    with connect() as con:
        row = con.execute("SELECT logs FROM tasks WHERE id=?", (task_id,)).fetchone()
        entries = json.loads(row["logs"])
        entries.append({"time": now(), "message": str(message)[-12000:]})
        con.execute(
            "UPDATE tasks SET logs=?,updated_at=? WHERE id=?", (json.dumps(entries[-100:]), now(), task_id)
        )


def finish(task_id, status, result=None):
    with connect() as con:
        con.execute(
            "UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?",
            (status, json.dumps(result), now(), task_id),
        )
        row = con.execute("SELECT lab_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        run = con.execute("SELECT result FROM scenario_runs WHERE task_id=?", (task_id,)).fetchone()
        if run:
            combined = json.loads(run[0] or '{}')
            combined.update(result or {})
            if status == "interrupted":
                combined.setdefault("error", "Worker 中断；已完成步骤保留，后续状态未知，未自动重放")
            con.execute("UPDATE scenario_runs SET status=?,result=?,finished_at=? WHERE task_id=?",
                        (status, json.dumps(combined), now(), task_id))
            con.execute("DELETE FROM scenario_runs WHERE lab_id=(SELECT lab_id FROM scenario_runs WHERE task_id=?) "
                        "AND status NOT IN ('queued','running') AND task_id NOT IN ("
                        "SELECT task_id FROM scenario_runs WHERE lab_id=(SELECT lab_id FROM scenario_runs WHERE task_id=?) "
                        "AND status NOT IN ('queued','running') ORDER BY created_at DESC LIMIT 100)", (task_id, task_id))
        if row and status not in ("queued", "running"):
            prune_tasks(con, row[0])


TASK_HISTORY_PER_LAB = 500


def prune_tasks(con, lab_id):
    con.execute(
        "DELETE FROM tasks WHERE lab_id=? AND status NOT IN ('queued','running') AND id NOT IN ("
        "SELECT id FROM tasks WHERE lab_id=? AND status NOT IN ('queued','running') "
        "ORDER BY created_at DESC LIMIT ?)",
        (lab_id, lab_id, TASK_HISTORY_PER_LAB),
    )


def request_event(request_id, source, kind, node_id=None, evidence="live", timestamp=None, data=None):
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        request = con.execute("SELECT lab_id,trace FROM client_requests WHERE id=?", (request_id,)).fetchone()
        if not request or not request["trace"]:
            return
        con.execute(
            "INSERT INTO request_events(request_id,lab_id,source,kind,node_id,evidence,time,received_at,data) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                request["lab_id"],
                source,
                kind,
                node_id,
                evidence,
                timestamp or now(),
                now(),
                json.dumps(data or {}),
            ),
        )


def request_status(request_id, status, result=None):
    with connect() as con:
        con.execute(
            "UPDATE client_requests SET status=?,result=COALESCE(?,result),updated_at=? WHERE id=?",
            (status, json.dumps(result) if result is not None else None, now(), request_id),
        )
        # Keep the latest 100 completed requests per lab; never evict queued/running work.
        row = con.execute("SELECT lab_id FROM client_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return
        old = [
            r[0]
            for r in con.execute(
                "SELECT id FROM client_requests WHERE lab_id=? "
                "AND status NOT IN ('queued','running') ORDER BY created_at DESC LIMIT -1 OFFSET 100",
                (row[0],),
            )
        ]
        for old_id in old:
            con.execute("DELETE FROM browser_requests WHERE request_id=?", (old_id,))
            con.execute("DELETE FROM request_events WHERE request_id=?", (old_id,))
            con.execute("DELETE FROM client_requests WHERE id=?", (old_id,))


def deleting(con, lab_id):
    return con.execute("SELECT 1 FROM tasks WHERE lab_id=? AND kind='delete_lab' AND status IN ('queued','running')", (lab_id,)).fetchone() is not None


def erase_lab(lab_id):
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM browser_requests WHERE request_id IN (SELECT id FROM client_requests WHERE lab_id=?)", (lab_id,))
        for table in ("request_events", "client_requests", "entries", "response_versions", "response_state", "revisions", "tasks", "task_receipts", "lab_scenarios", "scenario_runs"):
            con.execute(f"DELETE FROM {table} WHERE lab_id=?", (lab_id,))
        con.execute("DELETE FROM labs WHERE id=?", (lab_id,))
        con.execute("UPDATE scenario_creations SET lab_id=NULL WHERE lab_id=?", (lab_id,))
        con.execute("DELETE FROM meta WHERE key LIKE ?", (f"node-ip-v2:{lab_id}:%",))
