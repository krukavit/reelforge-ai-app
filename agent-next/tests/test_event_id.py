import tempfile
from pathlib import Path

from app.state_store import StateStore


def test_add_event_returns_durable_event_id():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "event-id.db"
        store = StateStore(str(db))

        event_id = store.add_event(
            "test",
            "EVENT_ID_PROBE",
            "event-id-task",
            {"probe": True},
        )

        assert isinstance(event_id, int)
        assert event_id > 0

        with store._connect() as con:
            row = con.execute(
                "SELECT id, component, event_type, task_id "
                "FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()

        assert row is not None
        assert row["id"] == event_id
        assert row["component"] == "test"
        assert row["event_type"] == "EVENT_ID_PROBE"
        assert row["task_id"] == "event-id-task"


if __name__ == "__main__":
    test_add_event_returns_durable_event_id()
    print("EVENT_ID_TESTS=1/1")
    print("EVENT_ID_GREEN")
