import os
import sys

import tempfile
import uuid
from pathlib import Path

from app.runner import Runner
from app.state_store import StateStore


def make_runner():
    d = tempfile.TemporaryDirectory()
    r = Runner(StateStore(Path(d.name) / "state.db"))
    return r, d


def test_step_cannot_be_added_after_completion():
    r, d = make_runner()
    try:
        tid = "t-" + uuid.uuid4().hex
        r.create_task(tid)
        r.plan(tid)
        sid = r.add_step(tid, 1, "health")
        r.transition(tid, "RUNNING")
        r.state.update_step(sid, status="COMPLETED")
        r.transition(tid, "COMPLETED")
        try:
            r.add_step(tid, 2, "health")
        except ValueError as exc:
            assert "COMPLETED" in str(exc)
        else:
            raise AssertionError("step added to completed task")
    finally:
        d.cleanup()


def test_authorization_rejects_step_from_other_task():
    r, d = make_runner()
    try:
        a = "a-" + uuid.uuid4().hex
        b = "b-" + uuid.uuid4().hex
        r.create_task(a); r.plan(a)
        r.create_task(b); r.plan(b)
        sid = r.add_step(a, 1, "health")
        try:
            r.authorize_action(b, sid, "health")
        except ValueError as exc:
            assert "does not belong" in str(exc)
        else:
            raise AssertionError("cross-task step authorization accepted")
    finally:
        d.cleanup()


def test_authorization_rejects_completed_task():
    r, d = make_runner()
    try:
        tid = "c-" + uuid.uuid4().hex
        r.create_task(tid); r.plan(tid)
        sid = r.add_step(tid, 1, "health")
        r.transition(tid, "RUNNING")
        r.state.update_step(sid, status="COMPLETED")
        r.transition(tid, "COMPLETED")
        try:
            r.authorize_action(tid, sid, "health")
        except ValueError as exc:
            assert "COMPLETED" in str(exc)
        else:
            raise AssertionError("authorization accepted after completion")
    finally:
        d.cleanup()


def test_step_cannot_be_added_while_blocked_on_user_action():
    r, d = make_runner()
    try:
        tid = "blocked-" + uuid.uuid4().hex
        r.create_task(tid); r.plan(tid)
        sid = r.add_step(tid, 1, "delete")
        allowed, reason = r.authorize_action(tid, sid, "delete")
        assert (allowed, reason) == (False, "CONFIRM_REQUIRED")
        assert r.load_task(tid)["status"] == "BLOCKED_ON_USER_ACTION"
        try:
            r.add_step(tid, 2, "health")
        except ValueError as exc:
            assert "BLOCKED_ON_USER_ACTION" in str(exc)
        else:
            raise AssertionError("step added while confirmation was pending")
    finally:
        d.cleanup()


if __name__ == "__main__":
    test_step_cannot_be_added_after_completion()
    test_authorization_rejects_step_from_other_task()
    test_authorization_rejects_completed_task()
    test_step_cannot_be_added_while_blocked_on_user_action()
    print("RUNNER_INTEGRITY_OK")


def test_task_cannot_complete_with_incomplete_step():
    r, d = make_runner()
    try:
        tid = "incomplete-" + uuid.uuid4().hex
        r.create_task(tid); r.plan(tid)
        r.add_step(tid, 1, "health")
        r.transition(tid, "RUNNING")
        try:
            r.transition(tid, "COMPLETED")
        except ValueError as exc:
            assert "incomplete" in str(exc)
        else:
            raise AssertionError("task completed with incomplete step")
    finally:
        d.cleanup()
