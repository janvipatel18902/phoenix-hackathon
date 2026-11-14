from phoenix.models import ALLOWED_TRANSITIONS, TaskState


def test_terminal_states_only_escalate():
    # VERIFIED and NEEDS_HUMAN are terminal except for the universal "any -> NEEDS_HUMAN".
    assert ALLOWED_TRANSITIONS[TaskState.VERIFIED] == {TaskState.NEEDS_HUMAN}
    assert ALLOWED_TRANSITIONS[TaskState.NEEDS_HUMAN] == {TaskState.NEEDS_HUMAN}


def test_every_state_can_escalate():
    for state in TaskState:
        assert TaskState.NEEDS_HUMAN in ALLOWED_TRANSITIONS[state]


def test_core_pipeline_matches_contract():
    assert ALLOWED_TRANSITIONS[TaskState.PLANNED] >= {TaskState.INTENT_RECORDED}
    assert ALLOWED_TRANSITIONS[TaskState.INTENT_RECORDED] >= {TaskState.SENT}
    assert ALLOWED_TRANSITIONS[TaskState.SENT] >= {TaskState.UNKNOWN, TaskState.VERIFYING}
    assert ALLOWED_TRANSITIONS[TaskState.UNKNOWN] >= {TaskState.VERIFYING}
    assert ALLOWED_TRANSITIONS[TaskState.VERIFYING] >= {
        TaskState.VERIFIED,
        TaskState.FAILED_NOT_APPLIED,
        TaskState.NEEDS_HUMAN,
    }


def test_failed_not_applied_retries_same_key_path():
    # FAILED_NOT_APPLIED -> INTENT_RECORDED is the same-key retry (CONTRACTS.md section 3).
    assert ALLOWED_TRANSITIONS[TaskState.FAILED_NOT_APPLIED] >= {TaskState.INTENT_RECORDED}


def test_disallowed_shortcuts_are_absent():
    # Can never jump straight from PLANNED or INTENT_RECORDED to VERIFIED --
    # every effect must pass through VERIFYING (invariant 3).
    assert TaskState.VERIFIED not in ALLOWED_TRANSITIONS[TaskState.PLANNED]
    assert TaskState.VERIFIED not in ALLOWED_TRANSITIONS[TaskState.INTENT_RECORDED]
    assert TaskState.VERIFIED not in ALLOWED_TRANSITIONS[TaskState.SENT]
    assert TaskState.VERIFIED not in ALLOWED_TRANSITIONS[TaskState.UNKNOWN]
