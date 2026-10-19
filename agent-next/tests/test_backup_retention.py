import tempfile
from pathlib import Path

from app.backup_retention import BackupRetention
from app.state_store import StateStore


def test_retention_keeps_newest_and_deletes_old_files():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "state.db"
        root = Path(td) / "backups"
        root.mkdir()
        store = StateStore(str(db))
        paths = []
        for i in range(4):
            p = root / f"b{i}.tar.gz"
            p.write_text(str(i))
            paths.append(p)
            store.add_backup("project", str(p), "SUCCESS")
        report = BackupRetention(store, max_per_target=2).prune("project")
        assert len(report["deleted"]) == 2, report
        assert paths[2].exists() and paths[3].exists()
        assert not paths[0].exists() and not paths[1].exists()
        statuses = {r["backup_path"]: r["status"] for r in store.list_backups(target="project")}
        assert statuses[str(paths[0])] == "PRUNED"
        assert statuses[str(paths[1])] == "PRUNED"


def test_retention_never_deletes_symlink_or_directory():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "state.db"
        root = Path(td) / "backups"
        root.mkdir()
        real = root / "real.tar.gz"
        real.write_text("x")
        link = root / "link.tar.gz"
        link.symlink_to(real)
        directory = root / "old-dir"
        directory.mkdir()
        store = StateStore(str(db))
        store.add_backup("project", str(real), "SUCCESS")
        store.add_backup("project", str(link), "SUCCESS")
        store.add_backup("project", str(directory), "SUCCESS")
        report = BackupRetention(store, max_per_target=1).prune("project")
        assert link.exists()
        assert directory.exists()
        assert report["skipped"]


def test_executor_records_and_prunes_backup_outputs():
    import os
    import tempfile
    from app.executor import TaskExecutor

    with tempfile.TemporaryDirectory() as td:
        os.environ["AGENT_NEXT_BACKUP_MAX_PER_TARGET"] = "2"
        try:
            ex = TaskExecutor(state=StateStore(Path(td) / "state.db"))
            class FakeBackupWorker:
                def __init__(self):
                    self.n = 0
                def call_tool(self, capability, arguments=None, operation=None, *, validated=False, confirmed=False, task_id=None):
                    self.n += 1
                    Path(arguments["output"]).write_text(str(self.n))
                    return {"ok": True, "backup_path": arguments["output"]}
            ex.worker = FakeBackupWorker()
            for i in range(3):
                out = Path(td) / f"backup-{i}.tar.gz"
                result = ex.execute(
                    f"backup-exec-{i}", "backup",
                    arguments={"target": "home", "output": str(out)},
                    validated=True,
                )
                assert result["state"] == "COMPLETED", result
            files = sorted(Path(td).glob("backup-*.tar.gz"))
            assert len(files) == 2, files
            tracked = ex.runner.state.list_backups(target="home")
            assert sum(r["status"] == "SUCCESS" for r in tracked) == 2
            assert sum(r["status"] == "PRUNED" for r in tracked) == 1
        finally:
            os.environ.pop("AGENT_NEXT_BACKUP_MAX_PER_TARGET", None)


def test_retention_never_deletes_reused_newer_backup_path():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "state.db"
        p = Path(td) / "same.tar.gz"
        p.write_text("new")
        store = StateStore(str(db))
        store.add_backup("project", str(p), "SUCCESS")
        store.add_backup("project", str(p), "SUCCESS")
        report = BackupRetention(store, max_per_target=1).prune("project")
        assert p.exists()
        assert report["deleted"] == []
        assert report["pruned_records"] == 1
        rows = store.list_backups(target="project")
        assert rows[0]["status"] == "SUCCESS"
        assert rows[1]["status"] == "PRUNED"
