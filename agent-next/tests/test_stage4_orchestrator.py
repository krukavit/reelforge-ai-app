from app.orchestrator import AgentOrchestrator


def test_build_plan_creates_deterministic_stage1_workflows():
    orchestrator = AgentOrchestrator()
    plan = orchestrator.build_plan("inspect and fix ReelForge", profile="EDIT")
    assert plan["profile"] == "EDIT"
    assert plan["master_task_id"]
    assert plan["workflows"]
    names = [item["name"] for item in plan["workflows"]]
    assert names == [
        "BACKUP",
        "VERIFY_BACKUP",
        "DIAGNOSE",
        "READ",
        "PLAN",
        "EDIT",
        "TEST",
        "VERIFY",
    ]


def test_build_plan_contains_explicit_dependencies():
    orchestrator = AgentOrchestrator()
    plan = orchestrator.build_plan("inspect and fix ReelForge", profile="EDIT")
    workflows = plan["workflows"]
    assert workflows[0]["depends_on"] == []
    for index in range(1, len(workflows)):
        assert workflows[index]["depends_on"] == [workflows[index - 1]["task_id"]]


def test_plan_does_not_execute_workflows():
    orchestrator = AgentOrchestrator()
    plan = orchestrator.build_plan("inspect and fix ReelForge", profile="EDIT")
    assert plan["execution_started"] is False
    assert plan["mcp_calls"] == 0


def test_orchestrator_preserves_and_forwards_target_root():
    from app import master_workflow

    target_root = "/data/data/com.termux/files/home/reelforge-ai-app"
    orchestrator = AgentOrchestrator()
    plan = orchestrator.build_plan(
        "inspect and fix ReelForge",
        profile="EDIT",
        target_root=target_root,
    )

    assert plan["target_root"] == target_root

    captured = {}

    class FakeMasterWorkflowExecutor:
        def create_from_plan_atomic(self, received_plan, target_root=None):
            captured["plan"] = received_plan
            captured["target_root"] = target_root
            return {
                "master": {
                    "id": received_plan["master_task_id"],
                    "target_root": target_root,
                },
                "children": [],
            }

    original = master_workflow.MasterWorkflowExecutor
    master_workflow.MasterWorkflowExecutor = FakeMasterWorkflowExecutor
    try:
        result = orchestrator.create_from_plan(plan)
    finally:
        master_workflow.MasterWorkflowExecutor = original

    assert captured["plan"] is plan
    assert captured["target_root"] == target_root
    assert result["master"]["target_root"] == target_root


def test_create_master_from_plan_registers_only_workflows():
    orchestrator = AgentOrchestrator()
    plan = orchestrator.build_plan("inspect and fix ReelForge", profile="EDIT")
    result = orchestrator.create_from_plan(plan)
    assert result["master"]["id"] == plan["master_task_id"]
    assert len(result["children"]) == len(plan["workflows"])
    assert all(
        child["master_task"] == plan["master_task_id"]
        for child in result["children"]
    )


def test_orchestrator_target_root_persists_through_master_and_children():
    import tempfile
    from pathlib import Path

    from app.master_workflow import MasterWorkflowExecutor
    from app.state_store import StateStore

    with tempfile.TemporaryDirectory(prefix="stage4-target-root-") as tmp:
        base = Path(tmp)
        db = base / "state.db"
        target = base / "reelforge-project"
        target.mkdir()

        state = StateStore(path=db)
        orchestrator = AgentOrchestrator()

        plan = orchestrator.build_plan(
            "inspect and fix ReelForge",
            profile="EDIT",
            target_root=str(target),
        )

        master_executor = MasterWorkflowExecutor(state=state)
        original_create = MasterWorkflowExecutor

        from app import master_workflow
        original_class = master_workflow.MasterWorkflowExecutor
        master_workflow.MasterWorkflowExecutor = lambda: master_executor
        try:
            result = orchestrator.create_from_plan(plan)
        finally:
            master_workflow.MasterWorkflowExecutor = original_class

        expected_root = str(target.resolve())

        assert result["master"]["target_root"] == expected_root
        assert len(result["children"]) == len(plan["workflows"]) == 8
        assert all(
            child["target_root"] == expected_root
            for child in result["children"]
        )

        reloaded_master = state.get_task(plan["master_task_id"])
        assert reloaded_master["target_root"] == expected_root

        reloaded_children = [
            state.get_task(item["task_id"])
            for item in plan["workflows"]
        ]
        assert all(
            child["target_root"] == expected_root
            for child in reloaded_children
        )


def test_invalid_profile_is_rejected():
    orchestrator = AgentOrchestrator()
    failed = False
    try:
        AgentOrchestrator().build_plan("inspect and fix ReelForge", profile="INVALID")
    except ValueError:
        failed = True
    assert failed


def test_create_from_plan_rolls_back_partial_registration():
    import sqlite3
    import tempfile
    from pathlib import Path

    from app.state_store import StateStore
    from app.workflow import STAGE1_STEPS

    with tempfile.TemporaryDirectory(prefix="stage4-atomic-") as tmp:
        db = Path(tmp) / "state.db"
        state = StateStore(path=db)

        with sqlite3.connect(db) as con:
            con.execute(
                """
                CREATE TRIGGER fail_second_stage1_insert
                BEFORE INSERT ON tasks
                WHEN NEW.id = 'stage1-atomic-2'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic stage4 registration failure');
                END
                """
            )

        plan = {
            "master_task_id": "master-stage4-atomic",
            "workflows": [
                {
                    "task_id": "stage1-atomic-1",
                    "profile": "EDIT",
                    "depends_on": [],
                    "step_names": list(STAGE1_STEPS),
                },
                {
                    "task_id": "stage1-atomic-2",
                    "profile": "EDIT",
                    "depends_on": ["stage1-atomic-1"],
                    "step_names": list(STAGE1_STEPS),
                },
            ],
        }

        failed = False
        try:
            state.create_master_with_stage1_plan_atomic(
                master_task_id=plan["master_task_id"],
                workflows=plan["workflows"],
            )
        except sqlite3.DatabaseError as exc:
            failed = "synthetic stage4 registration failure" in str(exc)

        assert failed
        assert state.get_task(plan["master_task_id"]) is None
        assert all(
            state.get_task(item["task_id"]) is None
            for item in plan["workflows"]
        )

        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM task_steps").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
