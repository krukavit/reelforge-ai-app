import os
import sys
import tempfile
from pathlib import Path


from app.confirmation import create_confirmation, consume_confirmation
from app.state_store import StateStore


def test_confirmation_survives_new_store_instance():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "state.db"
        first = StateStore(db)
        token = create_confirmation(
            first, "task-persist", "delete", operation="item",
            step_no=1, step_id=7, arguments={"id": 42},
        )
        second = StateStore(db)
        ok, reason = consume_confirmation(
            second, "task-persist", "delete", token,
            operation="item", step_id=7, arguments={"id": 42},
        )
        assert ok is True
        assert reason == "CONFIRMED"


def test_consumed_confirmation_stays_consumed_after_new_store():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "state.db"
        first = StateStore(db)
        token = create_confirmation(
            first, "task-persist-2", "delete", operation="item",
            step_no=1, step_id=8, arguments={"id": 99},
        )
        second = StateStore(db)
        assert consume_confirmation(
            second, "task-persist-2", "delete", token,
            operation="item", step_id=8, arguments={"id": 99},
        )[0]
        third = StateStore(db)
        ok, reason = consume_confirmation(
            third, "task-persist-2", "delete", token,
            operation="item", step_id=8, arguments={"id": 99},
        )
        assert ok is False
        assert reason == "ALREADY_CONSUMED"


if __name__ == "__main__":
    test_confirmation_survives_new_store_instance()
    test_consumed_confirmation_stays_consumed_after_new_store()
    print("CONFIRMATION_PERSISTENCE_OK")
