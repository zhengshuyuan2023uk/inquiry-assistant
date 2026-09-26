"""Local execution metadata, without prompts, customer bodies, or credentials."""
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone


def _connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        # Serialize this additive upgrade when activity and analysis start together.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE IF NOT EXISTS model_runs (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            runner TEXT NOT NULL, prompt_version TEXT NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT, duration_ms INTEGER, status TEXT NOT NULL, draft_id TEXT,
            error_category TEXT, input_messages INTEGER NOT NULL, input_characters INTEGER NOT NULL,
            requested_model TEXT
        )""")
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(model_runs)')}
        if 'requested_model' not in columns:
            connection.execute('ALTER TABLE model_runs ADD COLUMN requested_model TEXT')
        connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


def start_run(workspace, conversation_id, context, *, runner="codex_local", run_id=None):
    run_id = run_id or uuid.uuid4().hex
    with closing(_connect(workspace.db_path)) as conn, conn:
        conn.execute("INSERT INTO model_runs (id,company_id,conversation_id,runner,prompt_version,started_at,status,input_messages,input_characters,requested_model) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (run_id, workspace.company_id, conversation_id, runner, "inquiry-v2",
                      datetime.now(timezone.utc).isoformat(), "running", len(context["messages"]),
                      len(json.dumps(context, ensure_ascii=False)),
                      (context.get('request') or {}).get('model')))
    return run_id


def recover_workbench_runs(workspace):
    """Only reconcile runs linked by ID to interrupted workbench jobs.

    Independent command-line runs and older unlinked telemetry are untouched.
    Do not create the telemetry table merely by opening the workbench.
    """
    with closing(sqlite3.connect(workspace.db_path, timeout=10)) as conn, conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_runs'").fetchone():
            return
        conn.execute("""UPDATE model_runs SET status='failed',error_category='interrupted',finished_at=?
            WHERE company_id=? AND status='running' AND id IN
            (SELECT id FROM workbench_jobs WHERE company_id=? AND status='interrupted')""",
            (datetime.now(timezone.utc).isoformat(), workspace.company_id, workspace.company_id))


def finish_run(workspace, run_id, duration_ms, *, draft_id=None, error_category=None):
    with closing(_connect(workspace.db_path)) as conn, conn:
        conn.execute("UPDATE model_runs SET finished_at=?,duration_ms=?,status=?,draft_id=?,error_category=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), duration_ms,
                      "succeeded" if draft_id else "failed", draft_id, error_category, run_id))


def list_runs(workspace):
    with closing(_connect(workspace.db_path)) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM model_runs ORDER BY started_at DESC LIMIT 100")]
    return rows
