"""SQLite store for applications, their logs, and answers Nate has approved."""
import datetime as dt
import json
import sqlite3
import threading

from . import config

STATES = (
    "DISCOVERED", "QUEUED", "RUNNING", "WAITING_FOR_INPUT",
    "READY_FOR_REVIEW", "SUBMITTED", "FAILED", "SKIPPED",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    application_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    company           TEXT DEFAULT '',
    position          TEXT DEFAULT '',
    url               TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'QUEUED',
    agent_id          TEXT DEFAULT '',
    resume_path       TEXT DEFAULT '',
    cover_letter_path TEXT DEFAULT '',
    started_at        TEXT,
    updated_at        TEXT,
    completed_at      TEXT,
    questions         TEXT DEFAULT '[]',
    answers           TEXT DEFAULT '{}',
    uncertainties     TEXT DEFAULT '[]',
    error             TEXT DEFAULT '',
    review_status     TEXT DEFAULT '',
    submission_status TEXT DEFAULT '',
    screenshot        TEXT DEFAULT '',
    current_url       TEXT DEFAULT '',
    claude_status     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER,
    agent_id       TEXT,
    ts             TEXT,
    message        TEXT
);
CREATE TABLE IF NOT EXISTS approved_answers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    question   TEXT NOT NULL,
    norm       TEXT NOT NULL UNIQUE,
    answer     TEXT NOT NULL,
    company    TEXT DEFAULT '',
    source     TEXT DEFAULT 'you',
    created_at TEXT
);
"""

JSON_COLS = {"questions", "answers", "uncertainties"}
_lock = threading.Lock()
_conn = None


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def conn():
    global _conn
    if _conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        for table, col in (("applications", "claude_status TEXT DEFAULT ''"),
                           ("approved_answers", "source TEXT DEFAULT 'you'")):
            try:  # databases made before the column existed
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        _conn.commit()
    return _conn


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for c in JSON_COLS:
        try:
            d[c] = json.loads(d[c] or ("{}" if c == "answers" else "[]"))
        except json.JSONDecodeError:
            d[c] = [] if c != "answers" else {}
    return d


def add_application(url, company="", position="", resume_path="", cover_letter_path="", status="QUEUED"):
    with _lock:
        c = conn()
        dup = c.execute(
            "SELECT application_id FROM applications WHERE url=? AND status NOT IN ('SKIPPED','FAILED')", (url,)
        ).fetchone()
        if dup:
            return None
        cur = c.execute(
            "INSERT INTO applications (company, position, url, status, resume_path, cover_letter_path, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (company, position, url, status, resume_path, cover_letter_path, now()),
        )
        c.commit()
        return cur.lastrowid


def update(app_id, **fields):
    if not fields:
        return
    fields["updated_at"] = now()
    if fields.get("status") in ("SUBMITTED", "SKIPPED", "FAILED"):
        fields.setdefault("completed_at", now())
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(json.dumps(v) if k in JSON_COLS else v)
    with _lock:
        conn().execute(f"UPDATE applications SET {', '.join(cols)} WHERE application_id=?", (*vals, app_id))
        conn().commit()


def get(app_id):
    with _lock:
        return _row(conn().execute("SELECT * FROM applications WHERE application_id=?", (app_id,)).fetchone())


def all_applications():
    with _lock:
        return [_row(r) for r in conn().execute("SELECT * FROM applications ORDER BY application_id DESC")]


def next_queued(exclude=()):
    with _lock:
        rows = conn().execute(
            "SELECT application_id FROM applications WHERE status='QUEUED' ORDER BY application_id"
        ).fetchall()
    for r in rows:
        if r[0] not in exclude:
            return r[0]
    return None


def counts():
    with _lock:
        rows = conn().execute("SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall()
    out = {s: 0 for s in STATES}
    out.update({r[0]: r[1] for r in rows})
    out["TOTAL"] = sum(r[1] for r in rows)
    return out


def reset_interrupted():
    """After a restart no browser is alive, so RUNNING work goes back in the queue."""
    with _lock:
        conn().execute("UPDATE applications SET status='QUEUED', agent_id='' WHERE status='RUNNING'")
        conn().commit()


def log(app_id, agent_id, message):
    with _lock:
        conn().execute(
            "INSERT INTO logs (application_id, agent_id, ts, message) VALUES (?,?,?,?)",
            (app_id, agent_id, now(), message),
        )
        conn().commit()


def logs(app_id=None, limit=200):
    with _lock:
        if app_id is None:
            rows = conn().execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn().execute(
                "SELECT * FROM logs WHERE application_id=? ORDER BY id DESC LIMIT ?", (app_id, limit)
            ).fetchall()
    return [dict(r) for r in reversed(rows)]


def save_answer(question, norm, answer, company="", source="you"):
    with _lock:
        conn().execute(
            "INSERT INTO approved_answers (question, norm, answer, company, source, created_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(norm) DO UPDATE SET answer=excluded.answer, question=excluded.question,"
            " company=excluded.company, source=excluded.source",
            (question, norm, answer, company, source, now()),
        )
        conn().commit()


def approved_answers():
    with _lock:
        return [dict(r) for r in conn().execute("SELECT * FROM approved_answers ORDER BY id")]


def delete_answer(answer_id):
    with _lock:
        conn().execute("DELETE FROM approved_answers WHERE id=?", (answer_id,))
        conn().commit()
