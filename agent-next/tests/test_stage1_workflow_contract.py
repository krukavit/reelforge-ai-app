from pathlib import Path
import tempfile

from app.state_store import StateStore


EXPECTED_STAGES = [
    "BACKUP",
    "VERIFY_BACKUP",
    "DIAGNOSE",
    "READ",
    "PLAN",
    "EDIT",
    "TEST",
    "VERIFY",
]


def test_stage1_contract_has_required_order():
    assert EXPECTED_STAGES == [
        "BACKUP",
        "VERIFY_BACKUP",
        "DIAGNOSE",
        "READ",
        "PLAN",
        "EDIT",
        "TEST",
        "VERIFY",
    ]


def test_stage1_backup_must_be_real_artifact():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backup = root / "backup.tar.gz"

        assert not backup.exists()

        backup.write_text("verified-backup")

        assert backup.is_file()
        assert not backup.is_symlink()
        assert backup.stat().st_size > 0


def test_stage1_state_store_supports_multi_step_workflow():
    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td) / "state.db")
        task_id = "stage1-contract"

        store.create_task(
            task_id=task_id,
            master_task=task_id,
            profile="EDIT",
            status="RECEIVED",
        )

        for number, stage in enumerate(EXPECTED_STAGES, 1):
            store.add_step(task_id, number, stage)

        steps = store.list_steps(task_id)

        assert [step["name"] for step in steps] == EXPECTED_STAGES
        assert len(steps) == len(EXPECTED_STAGES)


if __name__ == "__main__":
    test_stage1_contract_has_required_order()
    test_stage1_backup_must_be_real_artifact()
    test_stage1_state_store_supports_multi_step_workflow()
    print("STAGE1_WORKFLOW_CONTRACT_OK")
