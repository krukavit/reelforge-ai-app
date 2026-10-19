from app.recovery import RECOVERY_REASONS, mark_recovery


EXPECTED_RECOVERY_REASONS = {
    "PROCESS_RESTART",
    "BRIDGE_RESTART",
    "CORE_RESTART",
    "NETWORK_FAILURE",
    "USER_ACTION",
    "TOOL_FAILURE",
    "POLICY_BLOCK",
    "BACKUP_VERIFY_FAILED",
    "UNKNOWN",
}


def test_recovery_reason_contract_contains_all_defined_reasons():
    assert RECOVERY_REASONS == EXPECTED_RECOVERY_REASONS


def test_unknown_recovery_reason_normalizes_to_unknown():
    task = {"id": "reason-test"}
    result = mark_recovery(task, "NOT_A_REAL_REASON")
    assert result["recovery_reason"] == "UNKNOWN"


def test_known_recovery_reasons_are_preserved():
    for reason in EXPECTED_RECOVERY_REASONS:
        task = {"id": "reason-" + reason}
        result = mark_recovery(task, reason)
        assert result["recovery_reason"] == reason


def test_recovery_reason_contract_is_deterministic():
    assert sorted(RECOVERY_REASONS) == sorted(EXPECTED_RECOVERY_REASONS)


if __name__ == "__main__":
    test_recovery_reason_contract_contains_all_defined_reasons()
    test_unknown_recovery_reason_normalizes_to_unknown()
    test_known_recovery_reasons_are_preserved()
    test_recovery_reason_contract_is_deterministic()
    print("RECOVERY_REASON_MATRIX_TESTS=4/4")
    print("RECOVERY_REASON_MATRIX_GREEN")
