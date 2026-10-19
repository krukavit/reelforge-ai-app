"""Create bounded, informational fix proposals without applying changes."""
from __future__ import annotations

from pathlib import Path

from app.path_resolver import TargetRootResolver

MAX_EVIDENCE_CHARS = 2048
MAX_FIELD_CHARS = 1000
ALLOWED_CATEGORIES = {
    "TEST_FAILURE",
    "COMPILE_FAILURE",
    "TIMEOUT",
    "UNKNOWN_OUTCOME",
}
ALLOWED_ROOTS = {"app", "tests"}


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _resolve_target(target_root: object, file_path: object) -> str | None:
    if not isinstance(target_root, (str, Path)):
        return None
    if not isinstance(file_path, str) or not file_path:
        return None
    if "\\" in file_path:
        return None

    parts = file_path.split("/")
    if (
        len(parts) < 2
        or parts[0] not in ALLOWED_ROOTS
        or any(part in {"", ".", ".."} for part in parts)
        or not file_path.endswith(".py")
    ):
        return None

    try:
        resolver = TargetRootResolver(target_root)
        resolved = resolver.resolve(file_path, must_exist=True)
    except (OSError, ValueError, TypeError):
        return None

    if not resolved.is_file() or resolved.suffix != ".py":
        return None

    try:
        relative = resolved.relative_to(resolver.root)
    except ValueError:
        return None

    return relative.as_posix()


def _result(status: str, proposal: dict | None = None) -> dict:
    return {
        "status": status,
        "proposal": proposal,
        "execution_allowed": False,
        "apply_changes": False,
        "commands": [],
    }


def create_proposal(
    *,
    target_root: str | Path | None,
    category: str,
    evidence: str,
    file_path: str | None,
    rationale: str | None,
    expected_effect: str | None,
    verification: str | None,
) -> dict:
    """Build a bounded proposal only for an existing file inside target_root."""
    if category == "SUCCESS":
        return _result("NO_FIX_NEEDED")

    if category not in ALLOWED_CATEGORIES:
        return _result("INSUFFICIENT_EVIDENCE")

    resolved_path = _resolve_target(target_root, file_path)
    if resolved_path is None:
        return _result("INVALID_TARGET")

    bounded_evidence = _text(evidence, MAX_EVIDENCE_CHARS)
    bounded_rationale = _text(rationale, MAX_FIELD_CHARS)
    bounded_effect = _text(expected_effect, MAX_FIELD_CHARS)
    bounded_verification = _text(verification, MAX_FIELD_CHARS)

    if not all((
        bounded_evidence,
        bounded_rationale,
        bounded_effect,
        bounded_verification,
    )):
        return _result("INSUFFICIENT_EVIDENCE")

    proposal = {
        "category": category,
        "file_path": resolved_path,
        "evidence": bounded_evidence,
        "rationale": bounded_rationale,
        "expected_effect": bounded_effect,
        "verification": bounded_verification,
        "requires_human_review": True,
    }
    return _result("PROPOSAL_READY", proposal)
