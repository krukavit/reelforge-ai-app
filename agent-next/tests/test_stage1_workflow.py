import json
import tempfile
from pathlib import Path

from app.state_store import StateStore
from app.workflow import WorkflowExecutor, STAGE1_STEPS


def make_workflow(root):
    store = StateStore(Path(root) / "state.db")
    wf = WorkflowExecutor(state=store)
    created = wf.create_stage1("stage1-official", profile="EDIT")
    return store, wf, created


def test_stage1_creation_contract():
    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)

        assert created["stage"] == "STAGE1"
        assert created["status"] == "PLANNED"
        assert tuple(created["steps"]) == STAGE1_STEPS

        steps = store.list_steps("stage1-official")
        assert [step["name"] for step in steps] == list(STAGE1_STEPS)
        assert [step["status"] for step in steps] == ["RECEIVED"] * 8


def test_stage1_sequence_guard():
    for index, step_name in enumerate(STAGE1_STEPS):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "state.db")
            wf = WorkflowExecutor(state=store)
            task_id = f"stage1-sequence-{index}"
            wf.create_stage1(task_id, profile="EDIT")

            if index == 0:
                assert wf.require_previous_step_completed(
                    task_id, step_name
                ) is True
                continue

            previous = STAGE1_STEPS[index - 1]

            try:
                wf.require_previous_step_completed(task_id, step_name)
            except RuntimeError as exc:
                assert str(exc) == (
                    f"STEP_BLOCKED_PREVIOUS_NOT_COMPLETED:{previous}"
                )
            else:
                raise AssertionError(
                    f"{step_name} was not blocked before {previous}"
                )


def test_stage1_edit_requires_verified_backup():
    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)

        try:
            wf.require_verified_backup("stage1-official")
        except RuntimeError as exc:
            assert str(exc) == "EDIT_BLOCKED_BACKUP_NOT_COMPLETED"
        else:
            raise AssertionError("EDIT was not blocked without backup")

        steps = store.list_steps("stage1-official")
        by_name = {step["name"]: step for step in steps}

        store.update_step(by_name["BACKUP"]["id"], status="COMPLETED")

        try:
            wf.require_verified_backup("stage1-official")
        except RuntimeError as exc:
            assert str(exc) == "EDIT_BLOCKED_BACKUP_NOT_VERIFIED"
        else:
            raise AssertionError("EDIT was not blocked without verification")

        store.update_step(by_name["VERIFY_BACKUP"]["id"], status="COMPLETED")
        try:
            wf.require_verified_backup("stage1-official")
        except RuntimeError as exc:
            assert str(exc) == "EDIT_BLOCKED_BACKUP_EVIDENCE_INVALID"
        else:
            raise AssertionError("EDIT accepted completed steps without evidence")

        artifact = Path(td) / "verified-backup.bin"
        artifact.write_bytes(b"backup evidence")
        path = str(artifact)
        store.update_step(
            by_name["BACKUP"]["id"],
            result={"backup_path": path, "output": path, "target": "home"},
        )
        store.update_step(
            by_name["VERIFY_BACKUP"]["id"],
            result={"backup_path": path, "target": "home",
                    "verified": True, "reason": "BACKUP_VERIFIED"},
        )
        assert wf.require_verified_backup("stage1-official") is True


def test_stage1_direct_confirmation_bypass_is_blocked():
    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)

        steps = store.list_steps("stage1-official")
        by_name = {step["name"]: step for step in steps}

        for name in (
            "BACKUP",
            "VERIFY_BACKUP",
            "DIAGNOSE",
            "READ",
            "PLAN",
        ):
            store.update_step(by_name[name]["id"], status="COMPLETED")

        target = Path(td) / "must-not-exist.txt"

        class FakeWorker:
            def __init__(self):
                self.calls = 0

            def call_tool(
                self,
                capability,
                arguments=None,
                operation=None,
                *,
                validated=False,
                confirmed=False,
                task_id=None,
            ):
                self.calls += 1
                target.write_text("BYPASS")
                return {"ok": True}

        worker = FakeWorker()

        try:
            wf.execute_step(
                "stage1-official",
                "EDIT",
                "write",
                {"path": str(target), "content": "BYPASS"},
                worker=worker,
                confirmed=True,
            )
        except RuntimeError as exc:
            assert str(exc) == (
                "CONFIRMATION_REQUIRED_USE_WORKFLOW_CONFIRM_AND_EXECUTE"
            )
        else:
            raise AssertionError("direct confirmed execution was not blocked")

        assert worker.calls == 0
        assert not target.exists()


def test_stage1_execute_requires_runner_authorization():
    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)

        steps = store.list_steps("stage1-official")
        by_name = {step["name"]: step for step in steps}

        for name in (
            "BACKUP",
            "VERIFY_BACKUP",
            "DIAGNOSE",
            "READ",
            "PLAN",
        ):
            store.update_step(by_name[name]["id"], status="COMPLETED")

        # Satisfy the verified-backup precondition with temporary evidence.
        artifact = Path(td) / "verified-backup.bin"
        artifact.write_bytes(b"backup evidence")
        backup_path = str(artifact)

        store.update_step(
            by_name["BACKUP"]["id"],
            result={
                "backup_path": backup_path,
                "output": backup_path,
                "target": "home",
            },
        )
        store.update_step(
            by_name["VERIFY_BACKUP"]["id"],
            result={
                "backup_path": backup_path,
                "target": "home",
                "verified": True,
                "reason": "BACKUP_VERIFIED",
            },
        )

        class FakeWorker:
            def __init__(self):
                self.calls = 0

            def call_tool(
                self,
                capability,
                arguments=None,
                operation=None,
                *,
                validated=False,
                confirmed=False,
                task_id=None,
            ):
                self.calls += 1
                return {"ok": True}

        worker = FakeWorker()

        result = wf.execute_step(
            "stage1-official",
            "EDIT",
            "write",
            {"path": str(Path(td) / "target.txt"), "content": "x"},
            worker=worker,
        )

        assert result["allowed"] is False
        assert result["reason"] == "CONFIRM_REQUIRED"
        assert worker.calls == 0

        task = store.get_task("stage1-official")
        assert task["status"] == "BLOCKED_ON_USER_ACTION"

        edit = by_name["EDIT"]
        assert edit["status"] == "RECEIVED"

        refreshed = store.get_step(edit["id"])
        authorization = json.loads(refreshed["authorization"])
        assert authorization["capability"] == "write"
        assert authorization["reason"] == "CONFIRM_REQUIRED"


def test_stage1_policy_gate_reaches_confirmation():
    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)

        steps = store.list_steps("stage1-official")
        by_name = {step["name"]: step for step in steps}

        for name in (
            "BACKUP",
            "VERIFY_BACKUP",
            "DIAGNOSE",
            "READ",
            "PLAN",
        ):
            store.update_step(by_name[name]["id"], status="COMPLETED")

        # Supply valid temporary evidence so this test reaches the policy gate.
        artifact = Path(td) / "verified-backup.bin"
        artifact.write_bytes(b"backup evidence")
        backup_path = str(artifact)

        store.update_step(
            by_name["BACKUP"]["id"],
            result={
                "backup_path": backup_path,
                "output": backup_path,
                "target": "home",
            },
        )
        store.update_step(
            by_name["VERIFY_BACKUP"]["id"],
            result={
                "backup_path": backup_path,
                "target": "home",
                "verified": True,
                "reason": "BACKUP_VERIFIED",
            },
        )

        result = wf.authorize_workflow_step(
            "stage1-official",
            "EDIT",
            "write",
            {"unused": True},
        )

        assert result["allowed"] is False
        assert result["reason"] == "CONFIRM_REQUIRED"

        task = store.get_task("stage1-official")
        assert task["status"] == "BLOCKED_ON_USER_ACTION"


if __name__ == "__main__":
    test_stage1_creation_contract()
    test_stage1_sequence_guard()
    test_stage1_edit_requires_verified_backup()
    test_stage1_direct_confirmation_bypass_is_blocked()
    test_stage1_policy_gate_reaches_confirmation()
    print("STAGE1_OFFICIAL_TESTS_OK")


def test_stage1_diagnose_executes_and_persists_result():
    class FakeWorker:
        def __init__(self):
            self.calls = []

        def call_tool(
            self,
            capability,
            arguments=None,
            operation=None,
            *,
            validated=False,
            confirmed=False,
            task_id=None,
        ):
            self.calls.append({
                "capability": capability,
                "arguments": arguments,
                "operation": operation,
                "validated": validated,
                "confirmed": confirmed,
                "task_id": task_id,
            })
            return {
                "status": "success",
                "diagnose": {
                    "python": "ok",
                    "git": "ok",
                    "storage": "ok",
                },
            }

    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)
        steps = store.list_steps("stage1-official")
        by_name = {step["name"]: step for step in steps}

        store.update_step(
            by_name["BACKUP"]["id"],
            status="COMPLETED",
            result=json.dumps({"status": "success"}),
        )
        store.update_step(
            by_name["VERIFY_BACKUP"]["id"],
            status="COMPLETED",
            result=json.dumps({"status": "success", "verified": True}),
        )

        worker = FakeWorker()

        result = wf.execute_step(
            "stage1-official",
            "DIAGNOSE",
            "diagnose",
            {"intent": "all"},
            worker=worker,
        )

        assert result["allowed"] is True
        assert result["result"]["status"] == "success"
        assert result["result"]["diagnose"]["python"] == "ok"
        assert len(worker.calls) == 1

        call = worker.calls[0]
        assert call["capability"] == "diagnose"
        assert call["arguments"] == {"intent": "all"}
        assert call["validated"] is False
        assert call["confirmed"] is False

        loaded = wf.load("stage1-official")
        diagnose = next(
            step for step in loaded["steps"] if step["name"] == "DIAGNOSE"
        )

        assert diagnose["status"] == "COMPLETED"
        assert json.loads(diagnose["result"]) == result["result"]
        assert loaded["task"]["status"] == "RUNNING"


def test_stage1_read_executes_and_persists_result():
    class FakeWorker:
        def __init__(self):
            self.calls = []

        def call_tool(
            self,
            capability,
            arguments=None,
            operation=None,
            *,
            validated=False,
            confirmed=False,
            task_id=None,
        ):
            self.calls.append({
                "capability": capability,
                "arguments": arguments,
                "operation": operation,
                "validated": validated,
                "confirmed": confirmed,
                "task_id": task_id,
            })
            return {
                "status": "success",
                "path": arguments["path"],
                "start_line": arguments.get("start_line", 1),
                "max_lines": arguments.get("max_lines", 500),
                "content": "READ_OK",
            }

    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)
        steps = store.list_steps("stage1-official")
        by_name = {step["name"]: step for step in steps}

        for name in ("BACKUP", "VERIFY_BACKUP", "DIAGNOSE"):
            store.update_step(
                by_name[name]["id"],
                status="COMPLETED",
                result=json.dumps({"status": "success"}),
            )

        worker = FakeWorker()
        path = str(Path(td) / "target.txt")

        result = wf.execute_step(
            "stage1-official",
            "READ",
            "read",
            {"path": path, "start_line": 1, "max_lines": 500},
            worker=worker,
        )

        assert result["allowed"] is True
        assert result["result"]["status"] == "success"
        assert result["result"]["path"] == path
        assert len(worker.calls) == 1

        call = worker.calls[0]
        assert call["capability"] == "read"
        assert call["arguments"] == {
            "path": path,
            "start_line": 1,
            "max_lines": 500,
        }
        assert call["validated"] is False
        assert call["confirmed"] is False

        loaded = wf.load("stage1-official")
        read_step = next(
            step for step in loaded["steps"] if step["name"] == "READ"
        )

        assert read_step["status"] == "COMPLETED"
        assert json.loads(read_step["result"]) == result["result"]
        assert loaded["task"]["status"] == "RUNNING"


def test_stage1_execute_step_rejects_terminal_step_reexecution():
    class FakeWorker:
        def __init__(self):
            self.calls = []

        def call_tool(
            self,
            capability,
            arguments=None,
            operation=None,
            *,
            validated=False,
            confirmed=False,
            task_id=None,
        ):
            self.calls.append({
                "capability": capability,
                "arguments": arguments,
                "operation": operation,
                "validated": validated,
                "confirmed": confirmed,
                "task_id": task_id,
            })
            return {"status": "success", "path": arguments["path"]}

    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)
        steps = store.list_steps("stage1-official")
        by_name = {step["name"]: step for step in steps}

        for name in ("BACKUP", "VERIFY_BACKUP", "DIAGNOSE"):
            store.update_step(
                by_name[name]["id"],
                status="COMPLETED",
                result=json.dumps({"status": "success"}),
            )

        worker = FakeWorker()
        path = str(Path(td) / "target.txt")

        first = wf.execute_step(
            "stage1-official",
            "READ",
            "read",
            {"path": path, "start_line": 1, "max_lines": 20},
            worker=worker,
        )
        assert first["allowed"] is True
        assert len(worker.calls) == 1

        read_step = next(
            step for step in wf.load("stage1-official")["steps"]
            if step["name"] == "READ"
        )
        assert read_step["status"] == "COMPLETED"

        second = wf.execute_step(
            "stage1-official",
            "READ",
            "read",
            {"path": path, "start_line": 1, "max_lines": 20},
            worker=worker,
        )

        assert second["state"] == "COMPLETED"
        assert second["allowed"] is True
        assert second["reason"] == "STEP_ALREADY_COMPLETED"
        assert len(worker.calls) == 1

def test_stage1_plan_executes_without_worker_and_persists_result():
    class ForbiddenWorker:
        def call_tool(self, *args, **kwargs):
            raise AssertionError("PLAN_MUST_NOT_CALL_WORKER")

    with tempfile.TemporaryDirectory() as td:
        store, wf, created = make_workflow(td)
        steps = store.list_steps("stage1-official")
        by_name = {step["name"]: step for step in steps}

        for name in ("BACKUP", "VERIFY_BACKUP", "DIAGNOSE", "READ"):
            store.update_step(
                by_name[name]["id"],
                status="COMPLETED",
                result=json.dumps({"status": "success"}),
            )

        plan = {
            "objective": "prepare controlled edit",
            "actions": [
                {"step": "EDIT", "capability": "write"},
                {"step": "TEST", "capability": "health"},
            ],
        }

        result = wf.execute_step(
            "stage1-official",
            "PLAN",
            None,
            plan,
            worker=ForbiddenWorker(),
        )

        assert result["allowed"] is True
        assert result["result"] == plan
        assert result["state"] == "RUNNING"

        loaded = wf.load("stage1-official")
        plan_step = next(
            step for step in loaded["steps"] if step["name"] == "PLAN"
        )

        assert plan_step["status"] == "COMPLETED"
        assert json.loads(plan_step["result"]) == plan
        assert loaded["task"]["status"] == "RUNNING"


def test_stage1_backup_then_verify_uses_worker_once():
    import tempfile
    from pathlib import Path

    from app.state_store import StateStore
    from app.workflow import WorkflowExecutor

    class FakeWorker:
        def __init__(self, artifact):
            self.artifact = artifact
            self.calls = []

        def call_tool(self, capability, arguments, operation=None,
                      validated=False, confirmed=False, task_id=None):
            self.calls.append({
                "capability": capability,
                "arguments": arguments,
                "validated": validated,
            })
            return {
                "status": "success",
                "backup_path": str(self.artifact),
                "output": str(self.artifact),
                "target": arguments.get("target", "home"),
            }

    with tempfile.TemporaryDirectory() as td:
        artifact = Path(td) / "stage1-backup.tar.gz"
        artifact.write_bytes(b"verified-backup")

        state = StateStore(Path(td) / "state.db")
        wf = WorkflowExecutor(state=state)
        worker = FakeWorker(artifact)
        task_id = "stage1-official-backup-verify"

        wf.create_stage1(task_id)

        backup = wf.execute_step(
            task_id,
            "BACKUP",
            capability="backup",
            arguments={"target": "home", "output": str(artifact)},
            validated=True,
            worker=worker,
        )

        verify = wf.verify_backup(task_id)
        loaded = wf.load(task_id)

        assert backup["state"] == "RUNNING"
        assert verify["reason"] == "BACKUP_VERIFIED"
        assert verify["result"]["verified"] is True
        assert loaded["task"]["status"] == "RUNNING"
        assert next(
            s["status"] for s in loaded["steps"] if s["name"] == "BACKUP"
        ) == "COMPLETED"
        assert next(
            s["status"] for s in loaded["steps"]
            if s["name"] == "VERIFY_BACKUP"
        ) == "COMPLETED"
        assert len(worker.calls) == 1


def test_stage1_backup_verify_preserves_target_when_core_omits_metadata():
    import tempfile
    from pathlib import Path

    from app.state_store import StateStore
    from app.workflow import WorkflowExecutor

    class CoreShapedWorker:
        def __init__(self):
            self.calls = []

        def call_tool(self, capability, arguments, operation=None,
                      validated=False, confirmed=False, task_id=None):
            self.calls.append({
                "capability": capability,
                "arguments": arguments,
                "validated": validated,
            })
            return {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Backing up configs...",
                        }
                    ]
                },
            }

    with tempfile.TemporaryDirectory() as td:
        artifact = Path(td) / "stage1-configs-backup.tar.gz"
        artifact.write_bytes(b"verified-backup")

        state = StateStore(Path(td) / "state.db")
        wf = WorkflowExecutor(state=state)
        worker = CoreShapedWorker()
        task_id = "stage1-core-shaped-backup-target"

        wf.create_stage1(task_id)

        backup = wf.execute_step(
            task_id,
            "BACKUP",
            capability="backup",
            arguments={
                "target": "configs",
                "output": str(artifact),
            },
            validated=True,
            worker=worker,
        )

        verify = wf.verify_backup(task_id)
        records = state.list_backups(
            target="configs",
            status="SUCCESS",
        )

        loaded = wf.load(task_id)
        backup_step = next(
            s for s in loaded["steps"] if s["name"] == "BACKUP"
        )

        assert backup["state"] == "RUNNING"
        assert verify["reason"] == "BACKUP_VERIFIED"
        assert verify["result"]["target"] == "configs"
        assert verify["result"]["backup_path"] == str(artifact)
        assert len(records) == 1
        assert records[0]["target"] == "configs"
        assert records[0]["backup_path"] == str(artifact)
        assert json.loads(backup_step["result"])["target"] == "configs"
        assert json.loads(backup_step["result"])["output"] == str(artifact)
        assert len(worker.calls) == 1


def test_stage1_backup_verify_failure_fails_task():
    import tempfile
    from pathlib import Path

    from app.state_store import StateStore
    from app.workflow import WorkflowExecutor

    class FakeWorker:
        def __init__(self, missing_path):
            self.missing_path = missing_path
            self.calls = []

        def call_tool(self, capability, arguments, operation=None,
                      validated=False, confirmed=False, task_id=None):
            self.calls.append(capability)
            return {
                "status": "success",
                "backup_path": str(self.missing_path),
                "output": str(self.missing_path),
                "target": arguments.get("target", "home"),
            }

    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "missing-backup.tar.gz"

        state = StateStore(Path(td) / "state.db")
        wf = WorkflowExecutor(state=state)
        worker = FakeWorker(missing)
        task_id = "stage1-official-backup-verify-failure"

        wf.create_stage1(task_id)

        wf.execute_step(
            task_id,
            "BACKUP",
            capability="backup",
            arguments={"target": "home", "output": str(missing)},
            validated=True,
            worker=worker,
        )

        verify = wf.verify_backup(task_id)
        loaded = wf.load(task_id)

        assert verify["reason"] == "BACKUP_VERIFY_FAILED"
        assert verify["state"] == "FAILED"
        assert loaded["task"]["status"] == "FAILED"
        assert next(
            s["status"] for s in loaded["steps"]
            if s["name"] == "VERIFY_BACKUP"
        ) == "FAILED"
        assert len(worker.calls) == 1


def test_stage1_edit_confirmation_resume_completes_step_and_blocks_replay():
    class FakeWorker:
        def __init__(self):
            self.calls = []

        def call_tool(
            self,
            capability,
            arguments=None,
            operation=None,
            *,
            validated=False,
            confirmed=False,
            task_id=None,
        ):
            self.calls.append({
                "capability": capability,
                "arguments": arguments,
                "operation": operation,
                "validated": validated,
                "confirmed": confirmed,
                "task_id": task_id,
            })
            if capability == "write":
                path = Path(arguments["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(arguments["content"])
            return {"ok": True}

    with tempfile.TemporaryDirectory() as td:
        state = StateStore(Path(td) / "state.db")
        wf = WorkflowExecutor(state=state)
        task_id = "stage1-edit-confirmation-regression"
        target_root = Path(td) / "target-root"
        target_root.mkdir()
        target = target_root / "app" / "workflow-confirmed.txt"

        wf.create_stage1(task_id, target_root=target_root)

        steps = state.list_steps(task_id)
        by_name = {step["name"]: step for step in steps}

        for name in ("BACKUP", "VERIFY_BACKUP", "DIAGNOSE", "READ", "PLAN"):
            state.update_step(
                by_name[name]["id"],
                status="COMPLETED",
                result=json.dumps({"status": "success"}),
            )

        # Supply real, consistent backup evidence for the EDIT safety gate.
        backup_artifact = Path(td) / "confirmed-edit-backup.bin"
        backup_artifact.write_bytes(b"isolated test backup evidence")
        backup_path = str(backup_artifact)
        state.update_step(
            by_name["BACKUP"]["id"],
            result={
                "backup_path": backup_path,
                "output": backup_path,
                "target": "home",
            },
        )
        state.update_step(
            by_name["VERIFY_BACKUP"]["id"],
            result={
                "backup_path": backup_path,
                "target": "home",
                "verified": True,
                "reason": "BACKUP_VERIFIED",
            },
        )

        worker = FakeWorker()
        arguments = {
            "path": "app/workflow-confirmed.txt",
            "content": "WORKFLOW_CONFIRMED_PAYLOAD",
        }

        requested = wf.execute_step(
            task_id,
            "EDIT",
            "write",
            arguments,
            worker=worker,
            validated=True,
            reason="confirmation regression",
        )

        assert requested["allowed"] is False
        assert requested["reason"] == "CONFIRM_REQUIRED"
        assert requested["confirmation_token"]
        assert state.get_task(task_id)["status"] == "BLOCKED_ON_USER_ACTION"
        assert not target.exists()
        assert worker.calls == []

        fresh_wf = WorkflowExecutor(state=state)

        resumed = fresh_wf.confirm_and_execute_step(
            task_id,
            "EDIT",
            "write",
            requested["confirmation_token"],
            arguments=arguments,
            worker=worker,
        )

        assert resumed["allowed"] is True
        assert resumed["reason"] == "CONFIRMED"
        assert resumed["state"] == "RUNNING"
        assert target.read_text() == "WORKFLOW_CONFIRMED_PAYLOAD"
        assert target.is_file()
        assert not (Path(td) / "workflow-confirmed.txt").exists()

        edit_step = next(
            step for step in state.list_steps(task_id)
            if step["name"] == "EDIT"
        )
        assert edit_step["status"] == "COMPLETED"

        write_calls = [
            call for call in worker.calls
            if call["capability"] == "write"
        ]
        assert len(write_calls) == 1
        assert write_calls[0]["confirmed"] is True
        assert Path(write_calls[0]["arguments"]["path"]) == target
        assert write_calls[0]["arguments"]["path"].startswith(
            str(target_root) + "/"
        )

        replay = fresh_wf.confirm_and_execute_step(
            task_id,
            "EDIT",
            "write",
            requested["confirmation_token"],
            arguments=arguments,
            worker=worker,
        )

        assert replay["allowed"] is False
        assert replay["reason"] == "CONFIRMATION_TASK_NOT_BLOCKED"
        assert len([
            call for call in worker.calls
            if call["capability"] == "write"
        ]) == 1



def test_stage1_execute_verify_backup_uses_persisted_backup_output():
    class FakeWorker:
        def __init__(self, artifact):
            self.artifact = artifact
            self.calls = []

        def call_tool(self, capability, arguments, operation=None,
                      validated=False, confirmed=False, task_id=None):
            self.calls.append({
                "capability": capability,
                "arguments": dict(arguments or {}),
            })
            return {
                "status": "success",
                "backup_path": str(self.artifact),
                "output": str(self.artifact),
                "target": arguments.get("target", "home"),
            }

    with tempfile.TemporaryDirectory() as td:
        artifact = Path(td) / "persisted-backup.txt"
        artifact.write_bytes(b"verified-backup")

        state = StateStore(Path(td) / "state.db")
        wf = WorkflowExecutor(state=state)
        worker = FakeWorker(artifact)
        task_id = "stage1-execute-verify-persisted-output"

        wf.create_stage1(task_id, profile="EDIT")

        backup = wf.execute_step(
            task_id,
            "BACKUP",
            capability="backup",
            arguments={
                "target": "home",
                "output": str(artifact),
            },
            validated=True,
            reason="regression test",
            worker=worker,
        )

        assert backup["state"] == "RUNNING"
        assert backup["allowed"] is True
        assert len(worker.calls) == 1

        verify = wf.execute_step(
            task_id,
            "VERIFY_BACKUP",
            capability="backup",
            arguments={},
            validated=True,
            worker=worker,
        )

        assert verify["state"] == "COMPLETED"
        assert verify["allowed"] is True
        assert verify["reason"] == "BACKUP_VERIFIED"
        assert verify["result"]["backup_path"] == str(artifact)
        assert verify["result"]["target"] == "home"

        # VERIFY_BACKUP must validate the persisted BACKUP artifact locally;
        # it must not dispatch another MCP/worker backup call.
        assert len(worker.calls) == 1
        assert worker.calls[0]["capability"] == "backup"

        steps = state.list_steps(task_id)
        by_name = {step["name"]: step for step in steps}
        assert by_name["BACKUP"]["status"] == "COMPLETED"
        assert by_name["VERIFY_BACKUP"]["status"] == "COMPLETED"


def test_stage1_verify_backup_rejects_substituted_path():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        artifact = root / "requested-backup.bin"
        substituted = root / "substituted-backup.bin"
        artifact.write_bytes(b"original backup artifact")
        substituted.write_bytes(b"different backup artifact")

        task_id = "stage1-backup-path-substitution"
        store = StateStore(root / "state.db")
        wf = WorkflowExecutor(state=store)
        wf.create_stage1(task_id)

        class FakeWorker:
            def call_tool(self, capability, arguments, operation=None,
                          validated=False, confirmed=False, task_id=None):
                return {
                    "status": "success",
                    "backup_path": str(artifact),
                    "output": str(artifact),
                    "target": arguments.get("target", "home"),
                }

        backup_result = wf.execute_step(
            task_id,
            "BACKUP",
            capability="backup",
            arguments={"target": "home", "output": str(artifact)},
            validated=True,
            worker=FakeWorker(),
        )
        assert backup_result["allowed"] is True

        rejected = wf.verify_backup(task_id, backup_path=str(substituted))
        assert rejected["allowed"] is False
        assert rejected["reason"] == "BACKUP_PATH_MISMATCH"

        accepted = wf.verify_backup(task_id)
        assert accepted["allowed"] is True
        assert accepted["reason"] == "BACKUP_VERIFIED"
        assert accepted["result"]["backup_path"] == str(artifact)


def test_stage1_backup_verify_is_idempotent():
    import tempfile
    from pathlib import Path

    from app.state_store import StateStore
    from app.workflow import WorkflowExecutor

    class FakeWorker:
        def __init__(self, artifact):
            self.artifact = artifact
            self.calls = 0

        def call_tool(self, capability, arguments, operation=None,
                      validated=False, confirmed=False, task_id=None):
            self.calls += 1
            return {
                "status": "success",
                "backup_path": str(self.artifact),
                "output": str(self.artifact),
                "target": arguments.get("target", "home"),
            }

    with tempfile.TemporaryDirectory() as td:
        artifact = Path(td) / "stage1-idempotent-backup.tar.gz"
        artifact.write_bytes(b"verified-backup")

        state = StateStore(Path(td) / "state.db")
        wf = WorkflowExecutor(state=state)
        worker = FakeWorker(artifact)
        task_id = "stage1-official-backup-idempotent"

        wf.create_stage1(task_id)

        wf.execute_step(
            task_id,
            "BACKUP",
            capability="backup",
            arguments={"target": "home", "output": str(artifact)},
            validated=True,
            worker=worker,
        )

        first = wf.verify_backup(task_id)
        records_first = len(
            state.list_backups(target="home", status="SUCCESS")
        )

        second = wf.verify_backup(task_id)
        records_second = len(
            state.list_backups(target="home", status="SUCCESS")
        )

        assert first["reason"] == "BACKUP_VERIFIED"
        assert second["reason"] == "BACKUP_ALREADY_VERIFIED"
        assert worker.calls == 1
        assert records_first == records_second


def _seed_completed_local_step(store, task_id, step_name, result):
    store.update_task(task_id, status="RUNNING")
    step = next(
        item for item in store.list_steps(task_id)
        if item["name"] == step_name
    )
    store.update_step(
        step["id"], status="COMPLETED", result=result
    )
    return step


def test_completed_local_step_replay_rejects_wrong_capability():
    with tempfile.TemporaryDirectory() as td:
        store, wf, _ = make_workflow(td)
        result = {
            "passed": True, "returncode": 0,
            "timed_out": False, "mode": "compile",
        }
        step = _seed_completed_local_step(
            store, "stage1-official", "TEST", result
        )

        response = wf.execute_step(
            "stage1-official", "TEST", "wrong_capability",
            {"mode": "compile"}, "compile",
        )

        assert response["allowed"] is False
        assert response["reason"] == "DENY_BY_DEFAULT"
        saved = store.get_step(step["id"])
        assert json.loads(saved["result"]) == result


def test_completed_local_step_replay_rejects_invalid_arguments():
    with tempfile.TemporaryDirectory() as td:
        store, wf, _ = make_workflow(td)
        result = {
            "passed": True, "returncode": 0,
            "timed_out": False, "mode": "compile",
        }
        step = _seed_completed_local_step(
            store, "stage1-official", "TEST", result
        )

        response = wf.execute_step(
            "stage1-official", "TEST", "local_test",
            {"mode": "not_allowlisted"}, "not_allowlisted",
        )

        assert response["allowed"] is False
        assert response["reason"] == "LOCAL_STEP_ARGUMENTS_INVALID"
        saved = store.get_step(step["id"])
        assert json.loads(saved["result"]) == result


def test_completed_local_verify_replay_rejects_wrong_operation():
    with tempfile.TemporaryDirectory() as td:
        store, wf, _ = make_workflow(td)
        result = {"verified": True}
        step = _seed_completed_local_step(
            store, "stage1-official", "VERIFY", result
        )

        response = wf.execute_step(
            "stage1-official", "VERIFY", "local_verify",
            {}, "wrong_operation",
        )

        assert response["allowed"] is False
        assert response["reason"] == "LOCAL_STEP_OPERATION_MISMATCH"
        saved = store.get_step(step["id"])
        assert json.loads(saved["result"]) == result


def test_completed_local_step_replay_preserves_result():
    with tempfile.TemporaryDirectory() as td:
        store, wf, _ = make_workflow(td)
        result = {
            "passed": True, "returncode": 0,
            "timed_out": False, "mode": "compile",
        }
        _seed_completed_local_step(
            store, "stage1-official", "TEST", result
        )

        response = wf.execute_step(
            "stage1-official", "TEST", "local_test",
            {"mode": "compile"}, "compile",
        )

        assert response["allowed"] is True
        assert response["reason"] == "STEP_ALREADY_COMPLETED"
        assert response["result"] == result


def test_completed_local_step_replay_rejects_inactive_task():
    with tempfile.TemporaryDirectory() as td:
        store, wf, _ = make_workflow(td)
        result = {
            "passed": True, "returncode": 0,
            "timed_out": False, "mode": "compile",
        }
        step = next(
            item for item in store.list_steps("stage1-official")
            if item["name"] == "TEST"
        )
        # Deliberately create an inconsistent completed-step/PLANNED-task state.
        store.update_step(
            step["id"], status="COMPLETED", result=result
        )

        response = wf.execute_step(
            "stage1-official", "TEST", "local_test",
            {"mode": "compile"}, "compile",
        )

        assert response["allowed"] is False
        assert response["reason"] == "LOCAL_STEP_TASK_NOT_RUNNING"
        assert store.get_task("stage1-official")["status"] == "PLANNED"
        assert json.loads(store.get_step(step["id"])["result"]) == result

def _make_running_local_test_workflow(root):
    store, wf, _ = make_workflow(root)
    store.update_task("stage1-official", status="RUNNING")
    step = next(
        item for item in store.list_steps("stage1-official")
        if item["name"] == "TEST"
    )
    return store, wf, step


def test_incomplete_local_test_rejects_wrong_capability_before_sequence():
    with tempfile.TemporaryDirectory() as td:
        store, wf, step = _make_running_local_test_workflow(td)

        response = wf.execute_step(
            "stage1-official", "TEST", "wrong_capability",
            {"mode": "test", "test_name": "path_resolver"}, "test",
        )

        assert response["allowed"] is False
        assert response["reason"] == "DENY_BY_DEFAULT"
        assert store.get_step(step["id"])["status"] == "RECEIVED"


def test_incomplete_local_test_rejects_invalid_arguments_before_sequence():
    with tempfile.TemporaryDirectory() as td:
        store, wf, step = _make_running_local_test_workflow(td)

        response = wf.execute_step(
            "stage1-official", "TEST", "local_test",
            {"mode": "invalid"}, "invalid",
        )

        assert response["allowed"] is False
        assert response["reason"] == "LOCAL_STEP_ARGUMENTS_INVALID"
        assert store.get_step(step["id"])["status"] == "RECEIVED"


def test_incomplete_local_test_rejects_unsupported_operation_before_sequence():
    with tempfile.TemporaryDirectory() as td:
        store, wf, step = _make_running_local_test_workflow(td)

        response = wf.execute_step(
            "stage1-official", "TEST", "local_test",
            {"mode": "test", "test_name": "path_resolver"}, "unsupported",
        )

        assert response["allowed"] is False
        assert response["reason"] == "LOCAL_STEP_OPERATION_MISMATCH"
        assert store.get_step(step["id"])["status"] == "RECEIVED"


def test_incomplete_local_test_valid_request_still_obeys_sequence():
    with tempfile.TemporaryDirectory() as td:
        store, wf, step = _make_running_local_test_workflow(td)

        try:
            wf.execute_step(
                "stage1-official", "TEST", "local_test",
                {"mode": "test", "test_name": "path_resolver"}, "test",
            )
        except RuntimeError as exc:
            assert str(exc) == "STEP_BLOCKED_PREVIOUS_NOT_COMPLETED:EDIT"
        else:
            raise AssertionError(
                "Valid TEST request bypassed the unfinished EDIT step"
            )

        assert store.get_step(step["id"])["status"] == "RECEIVED"
