import sqlite3
import tempfile
from pathlib import Path
import sys


from app.state_store import StateStore


def test_mcp_audit_v3_migrates_to_current_v6():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "legacy.db"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE tasks (
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
                error TEXT
            );
            CREATE TABLE mcp_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                task_id TEXT,
                tool_name TEXT NOT NULL,
                status INTEGER,
                result TEXT,
                error TEXT
            );
            INSERT INTO schema_meta(key,value) VALUES ('schema_version','3');
        """)
        con.commit(); con.close()
        store = StateStore(db)
        store.add_mcp_call("health", status="SUCCESS", arguments={"a": 1})
        row = store.list_mcp_calls(tool_name="health")[-1]
        assert row["arguments_hash"]
        assert "operation" in row
        assert "request_id" in row

        task_columns = {
            row[1]
            for row in store._connect().execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "target_root" in task_columns
        assert "metadata" in task_columns
        assert "recovery_reason" in task_columns
        assert store.get_schema_version() == "6"


if __name__ == "__main__":
    test_mcp_audit_v3_migrates_to_current_v6()
    print("MCP_AUDIT_V3_TO_V6_MIGRATION_OK")
