

import sys, os
from app.policy import authorize

def test_validated_flag_actually_propagates():
    # без validated=True — должно требовать подтверждения, не проходить
    ok, reason = authorize("backup", validated=False)
    assert ok is False and reason == "VALIDATION_REQUIRED"
    # с validated=True — должно реально долететь и пройти
    ok, reason = authorize("backup", validated=True)
    assert ok is True and reason == "VALIDATED"

def test_auto_never_needs_validated():
    ok, reason = authorize("health")
    assert ok is True and reason == "AUTO"

def test_confirm_requires_confirmed_not_validated():
    # validated=True не должен подменять собой confirmed для CONFIRM-уровня
    ok, reason = authorize("delete", validated=True, confirmed=False)
    assert ok is False and reason == "CONFIRM_REQUIRED"
    ok, reason = authorize("delete", confirmed=True)
    assert ok is True and reason == "CONFIRMED"

def test_always_block_ignores_both_flags():
    # ключевая проверка: run/session_run нельзя "пробить" никакими флагами
    for cap in ("run", "session_run"):
        ok, reason = authorize(cap, validated=True, confirmed=True)
        assert ok is False and reason == "ALWAYS_BLOCKED"

def test_composite_validated_propagates_through_operation():
    ok, reason = authorize("image_process", "resize", validated=True)
    assert ok is True and reason == "VALIDATED"
    ok, reason = authorize("image_process", "resize", validated=False)
    assert ok is False and reason == "VALIDATION_REQUIRED"

def test_git_pr_requires_confirmation_for_all_operations():
    for operation in ("list", "status", "view", "diff", "create", "merge", "approve"):
        ok, reason = authorize("git_pr", operation, validated=True, confirmed=False)
        assert ok is False and reason == "CONFIRM_REQUIRED"

        ok, reason = authorize("git_pr", operation, validated=True, confirmed=True)
        assert ok is True and reason == "CONFIRMED"


def test_composite_without_operation_denies_by_default():
    # найденная особенность: без operation composite-запись перекрывает
    # плоский уровень (image_process иначе был бы VALIDATED)
    ok, reason = authorize("image_process")
    assert ok is False and reason == "DENY_BY_DEFAULT"

def test_unknown_capability_denies_by_default():
    ok, reason = authorize("totally_made_up_capability")
    assert ok is False and reason == "DENY_BY_DEFAULT"
