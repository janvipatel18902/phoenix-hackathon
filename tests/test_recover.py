"""The recovery decision table, from CLAUDE.md:

| Found                  | External read says         | Action                     |
|------------------------|-----------------------------|-----------------------------|
| INTENT_RECORDED, never sent | not applied            | send (same key)             |
| SENT or UNKNOWN        | applied, matches expected   | mark VERIFIED, do not resend|
| SENT or UNKNOWN        | applied, differs             | NEEDS_HUMAN                 |
| SENT or UNKNOWN        | not applied                  | retry with same key         |
| any                    | cannot determine             | NEEDS_HUMAN                 |

decide_next_state() is the single source of truth for this table, reused by
both the worker's inline post-send verification and the startup recovery scan
(recover_task), so one test here covers both call sites.
"""

from phoenix.models import TaskState
from phoenix.recover import decide_next_state
from phoenix.verify import ExternalReadResult


def test_cannot_determine_is_needs_human():
    read = ExternalReadResult(applied=None, matches_expected=None)
    assert decide_next_state(read) == TaskState.NEEDS_HUMAN


def test_not_applied_is_failed_not_applied_for_same_key_retry():
    read = ExternalReadResult(applied=False, matches_expected=None)
    assert decide_next_state(read) == TaskState.FAILED_NOT_APPLIED


def test_applied_and_matches_is_verified():
    read = ExternalReadResult(applied=True, matches_expected=True)
    assert decide_next_state(read) == TaskState.VERIFIED


def test_applied_but_differs_is_needs_human_never_auto_corrected():
    # Invariant 5: applied-but-differs-from-expected => NEEDS_HUMAN, never
    # silently treated as success and never auto-corrected.
    read = ExternalReadResult(applied=True, matches_expected=False)
    assert decide_next_state(read) == TaskState.NEEDS_HUMAN


def test_applied_true_with_unknown_match_is_needs_human_not_verified():
    # Defensive: applied=True should always carry a matches_expected verdict.
    # If it somehow doesn't, never default to VERIFIED.
    read = ExternalReadResult(applied=True, matches_expected=None)
    assert decide_next_state(read) == TaskState.NEEDS_HUMAN
