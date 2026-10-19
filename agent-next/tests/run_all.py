import importlib.util
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
env = os.environ.copy()
env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

# These are standalone scripts with their own explicit __main__/top-level runner.
STANDALONE_TESTS = [
    "tests.run_policy_tests",
    "tests.run_confirmation_tests",
    "tests.test_confirmation_atomicity",
    "tests.test_confirmation_persistence",
    "tests.test_lifecycle_atomicity",
    "tests.test_executor_confirmation_restart",
    "tests.test_runner_integrity",
    "tests.test_runner_confirmation_gate",
    "tests.test_mcp_audit_and_rpc_version",
    "tests.test_registry_and_rpc",
    "tests.test_capability_inventory",
    "tests.test_authorization_boundary",
    "tests.test_state_store_mcp_audit_migration",
    "tests.test_event_id",
    "tests.probe_workers",
    "tests.probe_recovery",
    "tests.probe_stage0_policy_paths",
]

# These files are pytest-style function collections and previously had no
# __main__ block. Running them as subprocesses therefore silently did nothing.
# Execute every test_* function directly so the archive's runner actually
# verifies the code it claims to verify.
FUNCTION_TESTS = [
    "tests/test_executor_validated_propagation.py",
    "tests/test_mcp_client_safety.py",
    "tests/test_state_store_wal.py",
    "tests/test_backup_retention.py",
    "tests/test_recovery_integrity.py",
    "tests/test_archive_integrity.py",
    "tests/test_stage1_workflow.py",
    "tests/test_stage4_orchestrator.py",
    "tests/test_path_resolver.py",
    "tests/test_target_root_executor_confirmation.py",
    "tests/test_conversation.py",
    "tests/test_conversation_confirmation.py",
    "tests/test_conversation_response.py",
    "tests/test_conversation_llm.py",
    "tests/test_reelforge_provider.py",
    "tests/test_voice_service.py",
    "tests/test_voice_capture.py",
    "tests/test_speech.py",
    "tests/test_tts.py",
]

def clean_test_state():
    state = ROOT / "state"
    registry = state / "capability_registry.json"
    live_db = state / "agent_next.db"
    # Never unlink SQLite WAL/SHM sidecars while the preserved DB may be open.
    live_db_sidecars = {
        state / "agent_next.db-wal",
        state / "agent_next.db-shm",
    }
    if not state.exists():
        return
    for child in state.iterdir():
        if child in {registry, live_db, *live_db_sidecars}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

def run_function_tests(path):
    clean_test_state()
    spec = importlib.util.spec_from_file_location(
        "agent_next_test_" + Path(path).stem, ROOT / path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tests = [
        name for name in dir(module)
        if name.startswith("test_") and callable(getattr(module, name))
    ]
    if not tests:
        raise AssertionError(f"{path}: no test_* functions discovered")

    passed = 0
    for name in tests:
        try:
            getattr(module, name)()
        except Exception:
            print(f"FAIL {path}::{name}")
            traceback.print_exc()
            raise
        print(f"PASS {path}::{name}")
        passed += 1

    clean_test_state()
    print(f"{path}: {passed} function tests passed")

for path in STANDALONE_TESTS:
    clean_test_state()
    print("\n===", path, "===")
    result = subprocess.run(
        [sys.executable, "-m", path],
        cwd=ROOT,
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)

for path in FUNCTION_TESTS:
    print("\n===", path, "===")
    run_function_tests(path)

clean_test_state()
print("\nALL_LOCAL_TESTS_OK")
