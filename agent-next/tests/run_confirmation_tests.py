

import os, sys, traceback
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from app.state_store import StateStore
from app.confirmation import create_confirmation, consume_confirmation

TEST_DB = os.path.join(PROJECT_ROOT, "state", "test_confirmations.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
store = StateStore(path=TEST_DB)

def test_valid_token_confirms_once():
    token = create_confirmation(store, "task-1", "delete", operation="file", arguments={"path": "A"})
    ok, reason = consume_confirmation(store, "task-1", "delete", token, operation="file", arguments={"path": "A"})
    assert ok is True and reason == "CONFIRMED", (ok, reason)

def test_token_cannot_be_reused():
    token = create_confirmation(store, "task-2", "delete")
    consume_confirmation(store, "task-2", "delete", token)
    ok, reason = consume_confirmation(store, "task-2", "delete", token)
    assert ok is False and reason == "ALREADY_CONSUMED", (ok, reason)

def test_wrong_token_rejected():
    create_confirmation(store, "task-3", "delete")
    ok, reason = consume_confirmation(store, "task-3", "delete", "made-up-token")
    assert ok is False and reason == "NO_PENDING_CONFIRMATION", (ok, reason)

def test_token_scoped_to_task_id():
    token = create_confirmation(store, "task-4", "delete")
    ok, reason = consume_confirmation(store, "task-5-different", "delete", token)
    assert ok is False and reason == "NO_PENDING_CONFIRMATION", (ok, reason)


def test_token_scoped_to_exact_arguments():
    token = create_confirmation(store, "task-args", "delete", arguments={"path": "A"})
    ok, reason = consume_confirmation(store, "task-args", "delete", token, arguments={"path": "B"})
    assert ok is False and reason == "NO_PENDING_CONFIRMATION", (ok, reason)

def test_no_confirmation_created_means_nothing_to_forge():
    # суть test_llm_cannot_forge_confirm: раз create_confirmation не вызывался,
    # никакой токен ничего не подтвердит - "user_confirmed=true" от LLM
    # не имеет за собой настоящего challenge, и этот путь всегда падает
    ok, reason = consume_confirmation(store, "task-never-challenged", "delete", "user_confirmed=true")
    assert ok is False and reason == "NO_PENDING_CONFIRMATION", (ok, reason)

tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
passed, failed = 0, 0
for fn in tests:
    try:
        fn()
        print(f"PASS  {fn.__name__}")
        passed += 1
    except AssertionError as e:
        print(f"FAIL  {fn.__name__}  {e}")
        failed += 1
    except Exception:
        print(f"ERROR {fn.__name__}")
        traceback.print_exc()
        failed += 1

print(f"\n{passed} passed, {failed} failed out of {len(tests)}")

for suffix in ("-wal", "-shm"):
    try:
        os.remove(TEST_DB + suffix)
    except FileNotFoundError:
        pass

sys.exit(1 if failed else 0)
