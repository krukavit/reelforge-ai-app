import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "6"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path=None):
        raw_path = path if path is not None else PROJECT_ROOT / "state" / "agent_next.db"
        self.path = Path(raw_path).expanduser()
        if not self.path.is_absolute():
            self.path = PROJECT_ROOT / self.path
        self.path = self.path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _init_db(self):
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    master_task TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT,
                    checkpoint TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result TEXT,
                    metadata TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS task_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step_no INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    authorization TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    result TEXT,
                    error TEXT,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );

                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    metadata TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    component TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    task_id TEXT,
                    details TEXT
                );

                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    component TEXT NOT NULL,
                    stage TEXT,
                    task_id TEXT,
                    error TEXT NOT NULL,
                    root_cause TEXT,
                    correction TEXT,
                    prevention_rule TEXT
                );

                CREATE TABLE IF NOT EXISTS backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    task_id TEXT,
                    target TEXT NOT NULL,
                    backup_path TEXT NOT NULL,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    task_id TEXT,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT
                );

                CREATE TABLE IF NOT EXISTS mcp_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    task_id TEXT,
                    tool_name TEXT NOT NULL,
                    operation TEXT,
                    arguments_hash TEXT NOT NULL DEFAULT '',
                    request_id INTEGER,
                    session_id TEXT,
                    protocol_version TEXT,
                    status TEXT,
                    result TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS file_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    task_id TEXT,
                    path TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT
                );

                CREATE TABLE IF NOT EXISTS confirmations (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    step_id INTEGER,
                    step_no INTEGER,
                    capability TEXT NOT NULL,
                    operation TEXT,
                    arguments_hash TEXT NOT NULL DEFAULT "",
                    token_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                INSERT OR REPLACE INTO schema_meta(key, value)
                VALUES ('schema_version', '4');

                INSERT OR REPLACE INTO schema_meta(key, value)
                VALUES ('database', 'reelforge-agent-next');

                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_steps_task_step_no
                ON task_steps(task_id, step_no);
            """)
            try:
                con.execute("ALTER TABLE tasks ADD COLUMN recovery_reason TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

            try:
                con.execute("ALTER TABLE tasks ADD COLUMN target_root TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

            try:
                con.execute("ALTER TABLE tasks ADD COLUMN metadata TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

            con.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES ('schema_version', '6')"
            )

            try:
                con.execute("ALTER TABLE workers ADD COLUMN heartbeat_at TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

            try:
                con.execute("ALTER TABLE confirmations ADD COLUMN arguments_hash TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

            try:
                con.execute("ALTER TABLE confirmations ADD COLUMN step_id INTEGER")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

            for column, definition in (
                ("operation", "TEXT"),
                ("arguments_hash", "TEXT NOT NULL DEFAULT ''"),
                ("request_id", "INTEGER"),
                ("session_id", "TEXT"),
                ("protocol_version", "TEXT"),
            ):
                try:
                    con.execute(f"ALTER TABLE mcp_calls ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
    @staticmethod
    def _json(value):
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def get_schema_version(self):
        with self._connect() as con:
            row = con.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return row[0] if row else None

    @staticmethod
    def _canonical_target_root(target_root):
        if target_root is None:
            return None
        from .path_resolver import TargetRootResolver
        return str(TargetRootResolver(target_root).root)

    def create_task(
        self,
        task_id,
        master_task,
        profile,
        status="RECEIVED",
        target_root=None,
    ):
        now = utc_now()
        canonical_root = self._canonical_target_root(target_root)

        with self._connect() as con:
            con.execute(
                """
                INSERT INTO tasks
                (id, master_task, profile, status, created_at, target_root)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, master_task, profile, status, now, canonical_root),
            )
        return self.get_task(task_id)

    def create_stage1_child_atomic(
        self,
        task_id,
        master_task_id,
        profile,
        dependencies,
        step_names,
        target_root=None,
    ):
        """Atomically create, plan, bind and register a Stage1 child workflow."""
        now = utc_now()
        canonical_root = self._canonical_target_root(target_root)
        metadata = self._json({"dependencies": list(dependencies)})

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")

            existing = con.execute(
                "SELECT id FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Task already exists: {task_id}")

            con.execute(
                """
                INSERT INTO tasks
                (id, master_task, profile, status, created_at, stage, checkpoint, metadata, target_root)
                VALUES (?, ?, ?, 'PLANNED', ?, 'STAGE1', 'WORKFLOW_CREATED', ?, ?)
                """,
                (task_id, master_task_id, profile, now, metadata, canonical_root),
            )

            con.execute(
                """
                INSERT INTO events
                (timestamp, component, event_type, task_id, details)
                VALUES (?, 'runner', 'TASK_CREATED', ?, ?)
                """,
                (now, task_id, self._json({"profile": profile})),
            )

            con.execute(
                """
                INSERT INTO events
                (timestamp, component, event_type, task_id, details)
                VALUES (?, 'runner', 'STATE_CHANGED', ?, ?)
                """,
                (
                    now,
                    task_id,
                    self._json({"from": "RECEIVED", "to": "PLANNED"}),
                ),
            )

            for step_no, name in enumerate(step_names, 1):
                con.execute(
                    """
                    INSERT INTO task_steps
                    (task_id, step_no, name, status, authorization)
                    VALUES (?, ?, ?, 'RECEIVED', ?)
                    """,
                    (task_id, step_no, name, None),
                )

            con.execute(
                """
                INSERT INTO events
                (timestamp, component, event_type, task_id, details)
                VALUES (?, 'workflow', 'WORKFLOW_CHECKPOINT', ?, ?)
                """,
                (
                    now,
                    task_id,
                    self._json({
                        "checkpoint": "WORKFLOW_CREATED",
                        "stage": "STAGE1",
                    }),
                ),
            )

        return self.get_task(task_id), self.list_steps(task_id)

    def create_master_with_stage1_plan_atomic(
        self,
        master_task_id,
        workflows,
        target_root=None,
    ):
        """Atomically register a master task and all Stage1 child workflows."""
        now = utc_now()
        canonical_root = self._canonical_target_root(target_root)

        if not workflows:
            raise ValueError("workflows must be non-empty")

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")

            existing = con.execute(
                "SELECT id FROM tasks WHERE id = ?",
                (master_task_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Task already exists: {master_task_id}")

            con.execute(
                """
                INSERT INTO tasks
                (id, master_task, profile, status, created_at, stage, checkpoint, target_root)
                VALUES (?, ?, 'MASTER', 'RECEIVED', ?, 'MASTER', 'WORKFLOW_CREATED', ?)
                """,
                (master_task_id, master_task_id, now, canonical_root),
            )

            con.execute(
                """
                INSERT INTO events
                (timestamp, component, event_type, task_id, details)
                VALUES (?, 'master_workflow', 'TASK_CREATED', ?, ?)
                """,
                (
                    now,
                    master_task_id,
                    self._json({"profile": "MASTER"}),
                ),
            )

            con.execute(
                """
                INSERT INTO events
                (timestamp, component, event_type, task_id, details)
                VALUES (?, 'master_workflow', 'WORKFLOW_CHECKPOINT', ?, ?)
                """,
                (
                    now,
                    master_task_id,
                    self._json({
                        "checkpoint": "WORKFLOW_CREATED",
                        "stage": "MASTER",
                    }),
                ),
            )

            for workflow in workflows:
                task_id = workflow["task_id"]
                profile = workflow["profile"]
                dependencies = list(workflow.get("depends_on", []))
                step_names = list(workflow["step_names"])

                existing = con.execute(
                    "SELECT id FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if existing is not None:
                    raise ValueError(f"Task already exists: {task_id}")

                metadata = self._json({
                    "dependencies": dependencies,
                })

                con.execute(
                    """
                    INSERT INTO tasks
                    (id, master_task, profile, status, created_at, stage, checkpoint, metadata, target_root)
                    VALUES (?, ?, ?, 'PLANNED', ?, 'STAGE1', 'WORKFLOW_CREATED', ?, ?)
                    """,
                    (task_id, master_task_id, profile, now, metadata, canonical_root),
                )

                con.execute(
                    """
                    INSERT INTO events
                    (timestamp, component, event_type, task_id, details)
                    VALUES (?, 'runner', 'TASK_CREATED', ?, ?)
                    """,
                    (
                        now,
                        task_id,
                        self._json({"profile": profile}),
                    ),
                )

                con.execute(
                    """
                    INSERT INTO events
                    (timestamp, component, event_type, task_id, details)
                    VALUES (?, 'runner', 'STATE_CHANGED', ?, ?)
                    """,
                    (
                        now,
                        task_id,
                        self._json({"from": "RECEIVED", "to": "PLANNED"}),
                    ),
                )

                for step_no, name in enumerate(step_names, 1):
                    con.execute(
                        """
                        INSERT INTO task_steps
                        (task_id, step_no, name, status, authorization)
                        VALUES (?, ?, ?, 'RECEIVED', ?)
                        """,
                        (task_id, step_no, name, None),
                    )

                con.execute(
                    """
                    INSERT INTO events
                    (timestamp, component, event_type, task_id, details)
                    VALUES (?, 'workflow', 'WORKFLOW_CHECKPOINT', ?, ?)
                    """,
                    (
                        now,
                        task_id,
                        self._json({
                            "checkpoint": "WORKFLOW_CREATED",
                            "stage": "STAGE1",
                        }),
                    ),
                )

        return self.get_task(master_task_id), [
            self.get_task(item["task_id"]) for item in workflows
        ]

    def update_task(self, task_id, **fields):
        allowed = {
            "master_task", "profile", "status", "stage", "checkpoint",
            "started_at", "finished_at", "result", "metadata", "error", "recovery_reason"
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown task fields: {sorted(unknown)}")

        if not fields:
            return self.get_task(task_id)

        values = []
        assignments = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(self._json(value) if key in {"result", "metadata"} else value)

        values.append(task_id)

        with self._connect() as con:
            cur = con.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cur.rowcount != 1:
                raise KeyError(f"Task not found: {task_id}")

        return self.get_task(task_id)

    def get_task(self, task_id):
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def add_event(self, component, event_type, task_id=None, details=None):
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO events
                (timestamp, component, event_type, task_id, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), component, event_type, task_id, self._json(details)),
            )
            return cur.lastrowid

    def add_step(self, task_id, step_no, name, status="RECEIVED",
                 authorization=None):
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO task_steps
                (task_id, step_no, name, status, authorization)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    step_no,
                    name,
                    status,
                    self._json(authorization),
                ),
            )
            return cur.lastrowid

    def update_step(self, step_id, **fields):
        allowed = {
            "status", "authorization", "started_at",
            "finished_at", "result", "error"
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown step fields: {sorted(unknown)}")

        if not fields:
            return self.get_step(step_id)

        assignments = []
        values = []

        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            if key in {"authorization", "result"}:
                value = self._json(value)
            values.append(value)

        values.append(step_id)

        with self._connect() as con:
            cur = con.execute(
                f"UPDATE task_steps SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cur.rowcount != 1:
                raise KeyError(f"Step not found: {step_id}")

        return self.get_step(step_id)

    def complete_master_reconcile_atomic(self, task_id, step_id, result):
        """Atomically complete the master RECONCILE step, task and lifecycle events."""
        now = utc_now()

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")

            task = con.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] != "RUNNING":
                raise RuntimeError(
                    f"Task {task_id} is not RUNNING (status={task['status']})"
                )

            steps = con.execute(
                "SELECT id, task_id, name, status FROM task_steps WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            if len(steps) != 1:
                raise ValueError(
                    f"Master task {task_id} must have exactly one RECONCILE step"
                )

            step = steps[0]
            if step["id"] != step_id or step["task_id"] != task_id:
                raise ValueError(
                    f"Step {step_id} does not match master task {task_id}"
                )
            if step["name"] != "RECONCILE":
                raise ValueError(f"Step {step_id} is not RECONCILE")
            if step["status"] not in {"RECEIVED", "RUNNING", "COMPLETED"}:
                raise RuntimeError(
                    f"RECONCILE step {step_id} has invalid status "
                    f"{step['status']}"
                )

            if step["status"] != "COMPLETED":
                con.execute(
                    """
                    UPDATE task_steps
                    SET status = 'COMPLETED', finished_at = ?, result = ?
                    WHERE id = ? AND task_id = ?
                    """,
                    (now, self._json(result), step_id, task_id),
                )

            remaining = con.execute(
                """
                SELECT COUNT(*) AS n
                FROM task_steps
                WHERE task_id = ? AND status != 'COMPLETED'
                """,
                (task_id,),
            ).fetchone()["n"]
            if remaining != 0:
                raise ValueError(
                    f"Master task {task_id} still has incomplete steps"
                )

            con.execute(
                """
                UPDATE tasks
                SET status = 'COMPLETED', finished_at = ?, result = ?
                WHERE id = ? AND status = 'RUNNING'
                """,
                (now, self._json(result), task_id),
            )

            con.execute(
                """
                INSERT INTO events
                    (timestamp, component, event_type, task_id, details)
                VALUES (?, 'runner', 'STATE_CHANGED', ?, ?)
                """,
                (
                    now,
                    task_id,
                    self._json({"from": "RUNNING", "to": "COMPLETED"}),
                ),
            )
            con.execute(
                """
                INSERT INTO events
                    (timestamp, component, event_type, task_id, details)
                VALUES (?, 'master_workflow', 'TASK_COMPLETED', ?, ?)
                """,
                (
                    now,
                    task_id,
                    self._json({
                        "step_id": step_id,
                        "reason": "ALL_CHILD_WORKFLOWS_COMPLETED",
                    }),
                ),
            )

        return self.get_task(task_id), self.get_step(step_id)

    def complete_step_and_task(self, task_id, step_id, result=None):
        """Atomically mark one step and its task COMPLETED.

        Prevents a crash between two independent writes from leaving a
        logically completed step attached to a non-completed task.
        """
        now = utc_now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task = con.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] != "RUNNING":
                raise RuntimeError(f"Task {task_id} is not RUNNING (status={task['status']})")
            step = con.execute("SELECT task_id, status FROM task_steps WHERE id = ?", (step_id,)).fetchone()
            if step is None:
                raise KeyError(f"Step not found: {step_id}")
            if step["task_id"] != task_id:
                raise ValueError(f"Step {step_id} does not belong to task {task_id}")
            if step["status"] == "COMPLETED":
                raise RuntimeError(f"Step {step_id} is already COMPLETED")
            con.execute(
                "UPDATE task_steps SET status='COMPLETED', finished_at=?, result=? WHERE id=?",
                (now, self._json(result), step_id),
            )
            remaining = con.execute(
                "SELECT COUNT(*) AS n FROM task_steps WHERE task_id = ? AND status != 'COMPLETED'",
                (task_id,),
            ).fetchone()["n"]
            if remaining == 0:
                con.execute(
                    "UPDATE tasks SET status='COMPLETED', finished_at=?, result=? WHERE id=?",
                    (now, self._json(result), task_id),
                )
        return self.get_task(task_id), self.get_step(step_id)

    def fail_step_and_task(self, task_id, step_id, error, recovery_reason=None):
        """Atomically mark one step and its task FAILED."""
        now = utc_now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task = con.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] != "RUNNING":
                raise RuntimeError(f"Task {task_id} is not RUNNING (status={task["status"]})")
            step = con.execute("SELECT task_id, status FROM task_steps WHERE id = ?", (step_id,)).fetchone()
            if step is None:
                raise KeyError(f"Step not found: {step_id}")
            if step["task_id"] != task_id:
                raise ValueError(f"Step {step_id} does not belong to task {task_id}")
            if step["status"] in {"COMPLETED", "FAILED"}:
                raise RuntimeError(f"Step {step_id} is terminal (status={step["status"]})")
            con.execute(
                "UPDATE task_steps SET status='FAILED', finished_at=?, error=? WHERE id=?",
                (now, str(error), step_id),
            )
            con.execute(
                "UPDATE tasks SET status='FAILED', finished_at=?, error=?, recovery_reason=? WHERE id=?",
                (now, str(error), recovery_reason, task_id),
            )
        return self.get_task(task_id), self.get_step(step_id)


    def recover_task_and_steps(self, task_id, error, recovery_reason):
        """Atomically fail a RUNNING task and every unfinished step.

        Completed steps remain completed; RECEIVED/RUNNING/WAITING steps are
        made terminal FAILED so a restart cannot leave a half-live task graph.
        """
        now = utc_now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task = con.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] != "RUNNING":
                raise RuntimeError(
                    f"Task {task_id} is not RUNNING (status={task['status']})"
                )
            con.execute(
                """UPDATE task_steps
                   SET status='FAILED', finished_at=?, error=?
                   WHERE task_id=? AND status != 'COMPLETED'""",
                (now, str(error), task_id),
            )
            con.execute(
                """UPDATE tasks
                   SET status='FAILED', finished_at=?, error=?, recovery_reason=?
                   WHERE id=? AND status='RUNNING'""",
                (now, str(error), recovery_reason, task_id),
            )
        return self.get_task(task_id), self.list_steps(task_id)

    def get_step(self, step_id):
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM task_steps WHERE id = ?", (step_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_steps(self, task_id):
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ?
                ORDER BY step_no, id
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_error(self, component, error, stage=None, task_id=None,
                  root_cause=None, correction=None, prevention_rule=None):
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO errors
                (timestamp, component, stage, task_id, error,
                 root_cause, correction, prevention_rule)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(), component, stage, task_id, error,
                    root_cause, correction, prevention_rule,
                ),
            )

    def add_backup(self, target, backup_path, status,
                   task_id=None):
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO backups
                (timestamp, task_id, target, backup_path, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), task_id, target, backup_path, status),
            )
            return cur.lastrowid

    def list_backups(self, target=None, status=None):
        where = []
        values = []
        if target is not None:
            where.append("target = ?")
            values.append(target)
        if status is not None:
            where.append("status = ?")
            values.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM backups" + clause + " ORDER BY id DESC", values
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_backup_status(self, backup_id, status):
        with self._connect() as con:
            cur = con.execute(
                "UPDATE backups SET status = ? WHERE id = ?",
                (status, backup_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"Backup not found: {backup_id}")

    def add_test(self, name, status, details=None, task_id=None):
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO tests
                (timestamp, task_id, name, status, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), task_id, name, status, self._json(details)),
            )

    @staticmethod
    def _arguments_hash(arguments):
        canonical = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def add_mcp_call(self, tool_name, status=None, result=None,
                     error=None, task_id=None, operation=None, arguments=None,
                     request_id=None, session_id=None, protocol_version=None):
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO mcp_calls
                (timestamp, task_id, tool_name, operation, arguments_hash,
                 request_id, session_id, protocol_version, status, result, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(), task_id, tool_name, operation,
                    self._arguments_hash(arguments), request_id, session_id,
                    protocol_version, status, self._json(result), error,
                ),
            )

    def list_mcp_calls(self, task_id=None, tool_name=None):
        where = []
        values = []
        if task_id is not None:
            where.append("task_id = ?")
            values.append(task_id)
        if tool_name is not None:
            where.append("tool_name = ?")
            values.append(tool_name)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM mcp_calls" + clause + " ORDER BY id ASC", values
            ).fetchall()
        return [dict(row) for row in rows]

    def add_file_change(self, path, action, details=None, task_id=None):
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO file_changes
                (timestamp, task_id, path, action, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), task_id, path, action, self._json(details)),
            )

    def add_worker(self, worker_id, task_id=None, status="RUNNING", pid=None, metadata=None):
        now = utc_now()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO workers
                (id, task_id, status, pid, started_at, heartbeat_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (worker_id, task_id, status, pid, now, now, self._json(metadata)),
            )
        return self.get_worker(worker_id)

    def get_worker(self, worker_id):
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_workers(self, status=None):
        query = "SELECT * FROM workers"
        params = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def heartbeat_worker(self, worker_id):
        with self._connect() as con:
            cur = con.execute(
                "UPDATE workers SET heartbeat_at = ? WHERE id = ? AND status = 'RUNNING'",
                (utc_now(), worker_id),
            )
            if cur.rowcount != 1:
                row = con.execute(
                    "SELECT status FROM workers WHERE id = ?", (worker_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Worker not found: {worker_id}")
                raise RuntimeError(
                    f"Worker {worker_id} is not RUNNING (status={row['status']})"
                )
        return self.get_worker(worker_id)

    def update_worker(self, worker_id, **fields):
        allowed = {"status", "finished_at", "metadata", "heartbeat_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown worker fields: {sorted(unknown)}")
        if not fields:
            return self.get_worker(worker_id)

        assignments = []
        values = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(self._json(value) if key == "metadata" else value)
        values.append(worker_id)

        with self._connect() as con:
            cur = con.execute(
                f"UPDATE workers SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        if cur.rowcount != 1:
            raise KeyError(f"Worker not found: {worker_id}")
        return self.get_worker(worker_id)
