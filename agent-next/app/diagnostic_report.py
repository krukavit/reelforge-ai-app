"""Read-only classification of local test-runner results.

This module does not execute commands, edit files, call MCP, or apply fixes.
Recommendations are informational only and never authorize execution.
"""
from __future__ import annotations

from app.local_test_runner import LocalTestResult, redact_output

MAX_EVIDENCE_CHARS = 2048


def diagnose_result(result: LocalTestResult) -> dict:
    """Return a bounded, deterministic diagnostic report for one run result."""
    if result.timed_out:
        category = "TIMEOUT"
        recommendation = (
            "Inspect the test for blocking or unexpectedly long work; "
            "review the timeout before deciding what to do next."
        )
        requires_human_review = True
    elif result.returncode is None:
        category = "UNKNOWN_OUTCOME"
        recommendation = (
            "The process outcome is unknown; verify execution status "
            "before interpreting the result or proposing a correction."
        )
        requires_human_review = True
    elif result.returncode == 0:
        category = "SUCCESS"
        recommendation = "No corrective action indicated by this result."
        requires_human_review = False
    elif result.mode == "compile":
        category = "COMPILE_FAILURE"
        recommendation = (
            "Review the compiler output and source location; "
            "propose any change separately for human review."
        )
        requires_human_review = True
    else:
        category = "TEST_FAILURE"
        recommendation = (
            "Inspect the failing test and its assertion evidence; "
            "prepare a separate proposal for human review if needed."
        )
        requires_human_review = True

    raw_evidence = "\\n".join(
        part for part in (result.stdout, result.stderr) if part
    )
    evidence = redact_output(raw_evidence)[:MAX_EVIDENCE_CHARS]

    return {
        "mode": result.mode,
        "category": category,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "recommendation": recommendation,
        "requires_human_review": requires_human_review,
        "evidence": evidence,
        "proposed_changes": [],
        "execution_allowed": False,
    }
