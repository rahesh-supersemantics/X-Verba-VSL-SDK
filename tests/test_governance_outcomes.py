import pytest

from vsl import Outcome
from vsl_core.ledger import LedgerEntryType


# ======================================================================
# EXISTING PHASE 1 TESTS
# ======================================================================


@pytest.mark.asyncio
async def test_high_confidence_low_amount_proceeds(governance):

    decision = await governance.authorize(
        action="approve_expense",
        context={
            "confidence": 0.95,
            "amount": 150,
            "currency": "NZD",
        },
    )

    assert decision.outcome == Outcome.PROCEED
    assert decision.allowed is True
    assert decision.requires_human_review is False
    assert decision.suspended is False


@pytest.mark.asyncio
async def test_low_confidence_routes_to_human(governance):

    decision = await governance.authorize(
        action="approve_expense",
        context={
            "confidence": 0.51,
            "amount": 150,
            "currency": "NZD",
        },
    )

    assert decision.outcome == Outcome.HUMAN_QUEUE
    assert decision.allowed is False
    assert decision.requires_human_review is True
    assert decision.suspended is False


@pytest.mark.asyncio
async def test_high_amount_enters_suspended_state(governance):

    decision = await governance.authorize(
        action="approve_expense",
        context={
            "confidence": 0.95,
            "amount": 5000,
            "currency": "NZD",
        },
    )

    assert decision.outcome == Outcome.SUSPENDED
    assert decision.allowed is False
    assert decision.requires_human_review is False
    assert decision.suspended is True
    assert decision.terminal_state == "expense-approvals-suspended"


# ======================================================================
# PHASE 2.1
# LEDGER SEQUENCE VERIFICATION
# ======================================================================


@pytest.mark.asyncio
async def test_proceed_ledger_sequence(governance):

    await governance.authorize(
        action="approve_expense",
        context={
            "confidence": 0.95,
            "amount": 150,
            "currency": "NZD",
        },
    )

    # all_entries() returns a generator.
    # Convert it to a list so we can index and inspect entries.
    entries = list(
        governance.ledger.store.all_entries()
    )

    assert [entry.entry_type for entry in entries] == [
        LedgerEntryType.MONITOR,
        LedgerEntryType.PRE_NODE,
        LedgerEntryType.VERIFICATION,
    ]

    # PRE_NODE caused by MONITOR
    assert entries[1].caused_by == entries[0].entry_id

    # VERIFICATION caused by PRE_NODE
    assert entries[2].caused_by == entries[1].entry_id

    # Successful governance = SUFFICIENT
    assert entries[2].payload["result"] == "SUFFICIENT"


@pytest.mark.asyncio
async def test_pre_node_denial_ledger_sequence(governance):

    await governance.authorize(
        action="approve_expense",
        context={
            "confidence": 0.51,
            "amount": 150,
            "currency": "NZD",
        },
    )

    entries = list(
        governance.ledger.store.all_entries()
    )

    assert [entry.entry_type for entry in entries] == [
        LedgerEntryType.MONITOR,
        LedgerEntryType.PRE_NODE,
        LedgerEntryType.VERIFICATION,
    ]

    # PRE_NODE caused by MONITOR
    assert entries[1].caused_by == entries[0].entry_id

    # VERIFICATION caused by PRE_NODE
    assert entries[2].caused_by == entries[1].entry_id

    # IMPORTANT:
    # Ordinary PreNode denial is still SUFFICIENT.
    assert entries[2].payload["result"] == "SUFFICIENT"


@pytest.mark.asyncio
async def test_invariant_violation_ledger_sequence(governance):

    await governance.authorize(
        action="approve_expense",
        context={
            "confidence": 0.95,
            "amount": 5000,
            "currency": "NZD",
        },
    )

    entries = list(
        governance.ledger.store.all_entries()
    )

    assert [entry.entry_type for entry in entries] == [
        LedgerEntryType.MONITOR,
        LedgerEntryType.PRE_NODE,
        LedgerEntryType.VERIFICATION,
        LedgerEntryType.TERMINAL,
    ]

    # PRE_NODE caused by MONITOR
    assert entries[1].caused_by == entries[0].entry_id

    # VERIFICATION caused by PRE_NODE
    assert entries[2].caused_by == entries[1].entry_id

    # TERMINAL caused by VERIFICATION
    assert entries[3].caused_by == entries[2].entry_id

    # Governance failure = INSUFFICIENT
    assert entries[2].payload["result"] == "INSUFFICIENT"


# ======================================================================
# PHASE 2.1
# GOVERNED EXECUTION
# ======================================================================


@pytest.mark.asyncio
async def test_run_executes_effect_on_proceed(governance):

    calls = []

    result = await governance.run(
        action="approve_expense",
        context={
            "confidence": 0.95,
            "amount": 150,
            "currency": "NZD",
        },
        effect=lambda: calls.append("approved"),
    )

    assert result.decision.outcome == Outcome.PROCEED
    assert result.performed is True

    # list.append() returns None
    assert result.result is None

    assert calls == ["approved"]


@pytest.mark.asyncio
async def test_run_does_not_execute_effect_on_pre_node_denial(
    governance,
):

    calls = []

    result = await governance.run(
        action="approve_expense",
        context={
            "confidence": 0.51,
            "amount": 150,
            "currency": "NZD",
        },
        effect=lambda: calls.append("approved"),
    )

    assert result.decision.outcome == Outcome.HUMAN_QUEUE
    assert result.performed is False

    # CRITICAL:
    # The side effect must never execute.
    assert calls == []


@pytest.mark.asyncio
async def test_run_does_not_execute_effect_on_invariant_violation(
    governance,
):

    calls = []

    result = await governance.run(
        action="approve_expense",
        context={
            "confidence": 0.95,
            "amount": 5000,
            "currency": "NZD",
        },
        effect=lambda: calls.append("approved"),
    )

    assert result.decision.outcome == Outcome.SUSPENDED
    assert result.performed is False

    # CRITICAL:
    # The side effect must never execute.
    assert calls == []


# ======================================================================
# PHASE 2.1
# ASYNC EFFECT
# ======================================================================


@pytest.mark.asyncio
async def test_run_supports_async_effect(governance):

    calls = []

    async def approve():

        calls.append("approved")

        return "expense approved"

    result = await governance.run(
        action="approve_expense",
        context={
            "confidence": 0.95,
            "amount": 150,
            "currency": "NZD",
        },
        effect=approve,
    )

    assert result.decision.outcome == Outcome.PROCEED
    assert result.performed is True
    assert result.result == "expense approved"

    assert calls == ["approved"]


# ======================================================================
# PHASE 2.1
# EFFECT EXCEPTIONS MUST PROPAGATE
# ======================================================================


@pytest.mark.asyncio
async def test_run_propagates_effect_exception(governance):

    def failing_effect():

        raise RuntimeError(
            "payment service unavailable"
        )

    with pytest.raises(
        RuntimeError,
        match="payment service unavailable",
    ):

        await governance.run(
            action="approve_expense",
            context={
                "confidence": 0.95,
                "amount": 150,
                "currency": "NZD",
            },
            effect=failing_effect,
        )


# ======================================================================
# PHASE 2.1
# UNKNOWN ACTION FAILS CLOSED
# ======================================================================


@pytest.mark.asyncio
async def test_unknown_action_fails_closed(governance):

    with pytest.raises(
        ValueError,
        match="Unknown action",
    ):

        await governance.run(
            action="wire_transfer",
            context={
                "confidence": 0.95,
                "amount": 150,
                "currency": "NZD",
            },
            effect=lambda: None,
        )