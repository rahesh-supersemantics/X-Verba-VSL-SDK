from __future__ import annotations

from types import SimpleNamespace

import pytest

from vsl import Governance, Outcome

from vsl_core.conformance.reference_adapter import (
    PlainPythonReferenceAdapter,
)
from vsl_core.governance import GovernanceAuthority
from vsl_core.identity import Evidence
from vsl_core.ledger import (
    InMemoryLedgerStore,
    LedgerEntryType,
    VerbaLedger,
)
from vsl_core.constructs import (
    Fallback,
    Invariant,
    PreNode,
    TerminalState,
)
from vsl_core.metrics import (
    AssuranceBasis,
    F2Modification,
    GammaEstimate,
    ROBUST_GAMMA_DEFAULT_THRESHOLD,
)


# ================================================================
# TEST POLICY
# ================================================================

OUTPUT_LAYER = AssuranceBasis(
    f1_pre_commitment=True,
    f2_modification=F2Modification.NONE,
)


DECISION_THRESHOLD = 0.50
EXPENSE_CAP_NZD = 2000.0


def confidence_gamma(
    p: float,
    calibration_error: float = 0.10,
) -> GammaEstimate:
    """
    Convert confidence into the Gamma estimate expected by VSL-Core.
    """

    p = min(
        max(float(p), 1e-6),
        1 - 1e-6,
    )

    threshold = min(
        max(DECISION_THRESHOLD, 1e-6),
        1 - 1e-6,
    )

    return GammaEstimate(
        gamma_hat=(
            (p / (1 - p))
            / (threshold / (1 - threshold))
        ),
        delta_estimation_error=calibration_error,
        energy_gap_estimate=p - threshold,
    )


async def approval_confidence(
    context: dict,
) -> GammaEstimate:
    return confidence_gamma(
        context["confidence"]
    )


async def within_expense_cap(
    context: dict,
) -> bool:
    return (
        context["currency"] == "NZD"
        and float(context["amount"])
        <= EXPENSE_CAP_NZD
    )


EXPENSE_SUSPENDED = TerminalState(
    name="expense-approvals-suspended",
    description=(
        "Automated expense approval halted. "
        "Human re-enablement required."
    ),
    entry_conditions=(
        "expense-cap violated",
    ),
)


APPROVAL_CONFIDENCE = PreNode(
    name="approval-confidence",
    description=(
        "The model confidence must clear the "
        "robust Gamma threshold before "
        "auto-approval."
    ),
    monitor=approval_confidence,
    assurance_basis=OUTPUT_LAYER,
    gamma_threshold=ROBUST_GAMMA_DEFAULT_THRESHOLD,
    fallback=Fallback(
        on_failure="ROUTE_TO_HUMAN_QUEUE",
        max_retries=0,
    ),
)


EXPENSE_CAP = Invariant(
    name="expense-cap",
    description=(
        "No expense above the NZD cap is "
        "ever auto-approved."
    ),
    rule=within_expense_cap,
    assurance_basis=OUTPUT_LAYER,
    on_violation=EXPENSE_SUSPENDED,
)


class TestPolicy:
    """
    Minimal policy object compatible with the current
    Governance(policy, ledger) constructor.
    """

    agent = "expense-agent"

    actions = {
        "approve_expense": SimpleNamespace(
            pre_node=APPROVAL_CONFIDENCE,
            invariants=(
                EXPENSE_CAP,
            ),
        )
    }


# ================================================================
# FIXTURE
# ================================================================

@pytest.fixture
def governance() -> Governance:
    """
    Create a fresh Governance instance and in-memory ledger
    for every test.
    """

    ledger = VerbaLedger(
        InMemoryLedgerStore()
    )

    policy = TestPolicy()

    return Governance(
        policy=policy,
        ledger=ledger,
    )


# ================================================================
# HELPERS
# ================================================================

def ctx(
    confidence: float = 0.95,
    amount: float = 150,
) -> dict:
    return {
        "confidence": confidence,
        "amount": amount,
        "currency": "NZD",
    }


def entries(governance: Governance):
    """
    Return the ledger as a concrete list.

    all_entries() is an iterator/generator in VSL-Core.
    """

    return list(
        governance.ledger.store.all_entries()
    )


def entries_for_decision(
    governance: Governance,
    decision_id: str,
):
    return [
        entry
        for entry in entries(governance)
        if entry.decision_id == decision_id
    ]


def entry_types_for_decision(
    governance: Governance,
    decision_id: str,
):
    return [
        entry.entry_type
        for entry in entries_for_decision(
            governance,
            decision_id,
        )
    ]


# ================================================================
# PHASE 2.2
# LEDGER-DERIVED SUSPENSION
# ================================================================


@pytest.mark.asyncio
async def test_active_suspension_is_none_before_terminal(
    governance,
):
    assert (
        governance.active_suspension()
        is None
    )


@pytest.mark.asyncio
async def test_invariant_violation_creates_active_suspension(
    governance,
):
    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    assert decision.outcome is Outcome.SUSPENDED
    assert decision.suspended is True

    suspension_id = (
        governance.active_suspension()
    )

    assert suspension_id is not None

    terminal = next(
        entry
        for entry in entries(governance)
        if entry.entry_type
        == LedgerEntryType.TERMINAL
    )

    assert suspension_id == terminal.entry_id


@pytest.mark.asyncio
async def test_suspension_is_derived_from_ledger(
    governance,
):
    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    assert decision.suspended is True

    suspension_id = (
        governance.active_suspension()
    )

    assert suspension_id is not None

    # Remove any possibility that the result depends on
    # a simple in-memory "suspended" flag.
    #
    # active_suspension() must find the state by inspecting
    # the ledger.
    assert any(
        entry.entry_id == suspension_id
        and entry.entry_type
        == LedgerEntryType.TERMINAL
        for entry in entries(governance)
    )


@pytest.mark.asyncio
async def test_request_while_suspended_is_blocked(
    governance,
):
    first_decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    assert (
        first_decision.outcome
        is Outcome.SUSPENDED
    )

    second_decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=150,
        ),
    )

    assert (
        second_decision.outcome
        is Outcome.SUSPENDED
    )

    assert second_decision.allowed is False
    assert second_decision.suspended is True


@pytest.mark.asyncio
async def test_suspended_request_records_monitor_only(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    before = len(
        entries(governance)
    )

    suspension_id = (
        governance.active_suspension()
    )

    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=150,
        ),
    )

    assert decision.suspended is True

    after_entries = entries(
        governance
    )

    new_entries = after_entries[
        before:
    ]

    assert len(new_entries) == 1

    assert (
        new_entries[0].entry_type
        == LedgerEntryType.MONITOR
    )

    assert (
        new_entries[0]
        .payload["suspended_by"]
        == suspension_id
    )


@pytest.mark.asyncio
async def test_suspended_request_does_not_execute_effect(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    calls = []

    result = await governance.run(
        action="approve_expense",
        context=ctx(
            amount=150,
        ),
        effect=lambda: calls.append(
            "executed"
        ),
    )

    assert result.performed is False
    assert result.result is None
    assert result.decision.suspended is True
    assert calls == []


# ================================================================
# PHASE 2.3
# HUMAN RE-ENABLEMENT
# ================================================================


def finance_authority() -> GovernanceAuthority:
    return GovernanceAuthority(
        name="Finance Controls",
        escalation_contact="controls@example.com",
    )


def reenable_evidence() -> Evidence:
    return Evidence(
        root_cause_analysis=(
            "Expense amount was incorrectly classified "
            "during automated processing."
        ),
        specification_update_proposal=(
            "Update expense validation policy."
        ),
    )


def get_insufficient_verification(
    governance: Governance,
):
    return next(
        entry
        for entry in entries(governance)
        if (
            entry.entry_type
            == LedgerEntryType.VERIFICATION
            and entry.payload.get("result")
            == "INSUFFICIENT"
        )
    )


@pytest.mark.asyncio
async def test_reenable_requires_active_terminal(
    governance,
):
    with pytest.raises(
        ValueError,
        match="No active terminal state",
    ):
        governance.reenable(
            authority=finance_authority(),
            authorised_by="j.doe",
            evidence=reenable_evidence(),
        )


@pytest.mark.asyncio
async def test_reenable_requires_named_human(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    with pytest.raises(
        ValueError,
        match="authorised_by must not be empty",
    ):
        governance.reenable(
            authority=finance_authority(),
            authorised_by="",
            evidence=reenable_evidence(),
        )


@pytest.mark.asyncio
async def test_reenable_requires_valid_evidence(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    with pytest.raises(
        Exception,
    ):
        governance.reenable(
            authority=finance_authority(),
            authorised_by="j.doe",
            evidence=None,
        )


@pytest.mark.asyncio
async def test_reenable_resolves_terminal_state(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    terminal_id = (
        governance.active_suspension()
    )

    assert terminal_id is not None

    governance.reenable(
        authority=finance_authority(),
        authorised_by="j.doe",
        evidence=reenable_evidence(),
    )

    assert (
        governance.active_suspension()
        is None
    )


@pytest.mark.asyncio
async def test_reenable_writes_human_transition(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    terminal_id = (
        governance.active_suspension()
    )

    governance.reenable(
        authority=finance_authority(),
        authorised_by="j.doe",
        evidence=reenable_evidence(),
    )

    transitions = [
        entry
        for entry in entries(governance)
        if (
            entry.entry_type
            == LedgerEntryType.HUMAN_AUTHORISED_TRANSITION
        )
    ]

    assert len(transitions) == 1

    transition = transitions[0]

    assert (
        transition.caused_by
        == terminal_id
    )

    assert (
        transition.payload["authorised_by"]
        == "j.doe"
    )

    assert (
        transition.payload["authority"]
        == "Finance Controls"
    )


@pytest.mark.asyncio
async def test_reenable_writes_reenablement_entry(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    governance.reenable(
        authority=finance_authority(),
        authorised_by="j.doe",
        evidence=reenable_evidence(),
    )

    reenable_entries = [
        entry
        for entry in entries(governance)
        if (
            entry.entry_type
            == LedgerEntryType.RE_ENABLEMENT
        )
    ]

    assert len(reenable_entries) == 1

    reenable_entry = reenable_entries[0]

    assert (
        reenable_entry.payload[
            "new_instance_id"
        ]
    )

    assert (
        reenable_entry.payload[
            "new_identity_key"
        ]
    )

    assert (
        reenable_entry.payload[
            "predecessor_instance_id"
        ]
    )


@pytest.mark.asyncio
async def test_reenabled_governance_can_authorize_again(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    assert (
        governance.active_suspension()
        is not None
    )

    governance.reenable(
        authority=finance_authority(),
        authorised_by="j.doe",
        evidence=reenable_evidence(),
    )

    assert (
        governance.active_suspension()
        is None
    )

    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=150,
        ),
    )

    assert (
        decision.outcome
        is Outcome.PROCEED
    )

    assert decision.allowed is True


# ================================================================
# PHASE 2.4
# SPECIFICATION UPDATE
# ================================================================


@pytest.mark.asyncio
async def test_specification_update_requires_existing_verification(
    governance,
):
    with pytest.raises(
        ValueError,
        match="Verification entry not found",
    ):
        governance.record_specification_update(
            verification_entry_id="does-not-exist",
            new_policy_version="1.1.0",
            summary="Updated policy",
            approved_by="Finance Controls",
        )


@pytest.mark.asyncio
async def test_specification_update_requires_verification_entry(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    monitor = next(
        entry
        for entry in entries(governance)
        if entry.entry_type
        == LedgerEntryType.MONITOR
    )

    with pytest.raises(
        ValueError,
        match="must reference a VERIFICATION",
    ):
        governance.record_specification_update(
            verification_entry_id=monitor.entry_id,
            new_policy_version="1.1.0",
            summary="Updated policy",
            approved_by="Finance Controls",
        )


@pytest.mark.asyncio
async def test_specification_update_requires_insufficient_verification(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=150,
        ),
    )

    sufficient = next(
        entry
        for entry in entries(governance)
        if (
            entry.entry_type
            == LedgerEntryType.VERIFICATION
            and entry.payload.get("result")
            == "SUFFICIENT"
        )
    )

    with pytest.raises(
        ValueError,
        match="INSUFFICIENT verification",
    ):
        governance.record_specification_update(
            verification_entry_id=sufficient.entry_id,
            new_policy_version="1.1.0",
            summary="Updated policy",
            approved_by="Finance Controls",
        )


@pytest.mark.asyncio
async def test_specification_update_creates_ledger_entry(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    insufficient = (
        get_insufficient_verification(
            governance
        )
    )

    update_entry = (
        governance.record_specification_update(
            verification_entry_id=(
                insufficient.entry_id
            ),
            new_policy_version="1.1.0",
            summary=(
                "Updated expense validation "
                "policy."
            ),
            approved_by="Finance Controls",
        )
    )

    assert (
        update_entry.entry_type
        == LedgerEntryType.SPECIFICATION_UPDATE
    )

    assert (
        update_entry.caused_by
        == insufficient.entry_id
    )

    assert (
        update_entry.payload[
            "policy_version"
        ]
        == "1.1.0"
    )

    assert (
        update_entry.payload[
            "approved_by"
        ]
        == "Finance Controls"
    )

    assert (
        update_entry.payload[
            "status"
        ]
        == "approved"
    )


@pytest.mark.asyncio
async def test_specification_update_requires_policy_version(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    insufficient = (
        get_insufficient_verification(
            governance
        )
    )

    with pytest.raises(
        ValueError,
        match="new_policy_version",
    ):
        governance.record_specification_update(
            verification_entry_id=(
                insufficient.entry_id
            ),
            new_policy_version="",
            summary="Updated policy",
            approved_by="Finance Controls",
        )


@pytest.mark.asyncio
async def test_specification_update_requires_summary(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    insufficient = (
        get_insufficient_verification(
            governance
        )
    )

    with pytest.raises(
        ValueError,
        match="summary",
    ):
        governance.record_specification_update(
            verification_entry_id=(
                insufficient.entry_id
            ),
            new_policy_version="1.1.0",
            summary="",
            approved_by="Finance Controls",
        )


@pytest.mark.asyncio
async def test_specification_update_requires_approval(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    insufficient = (
        get_insufficient_verification(
            governance
        )
    )

    with pytest.raises(
        ValueError,
        match="approved_by",
    ):
        governance.record_specification_update(
            verification_entry_id=(
                insufficient.entry_id
            ),
            new_policy_version="1.1.0",
            summary="Updated policy",
            approved_by="",
        )


# ================================================================
# FULL PHASE 2.2 → 2.4 LIFECYCLE
# ================================================================


@pytest.mark.asyncio
async def test_complete_suspension_reenable_specification_lifecycle(
    governance,
):
    # ------------------------------------------------------------
    # STEP 1
    # Bad request violates invariant.
    # ------------------------------------------------------------

    suspended_decision = (
        await governance.authorize(
            action="approve_expense",
            context=ctx(
                amount=5000,
            ),
        )
    )

    assert (
        suspended_decision.outcome
        is Outcome.SUSPENDED
    )

    assert (
        governance.active_suspension()
        is not None
    )

    # ------------------------------------------------------------
    # STEP 2
    # Identify the insufficient verification.
    # ------------------------------------------------------------

    insufficient = (
        get_insufficient_verification(
            governance
        )
    )

    assert (
        insufficient.payload["result"]
        == "INSUFFICIENT"
    )

    # ------------------------------------------------------------
    # STEP 3
    # Human re-enablement.
    # ------------------------------------------------------------

    governance.reenable(
        authority=finance_authority(),
        authorised_by="j.doe",
        evidence=reenable_evidence(),
    )

    assert (
        governance.active_suspension()
        is None
    )

    # ------------------------------------------------------------
    # STEP 4
    # Record specification update.
    # ------------------------------------------------------------

    specification_update = (
        governance.record_specification_update(
            verification_entry_id=(
                insufficient.entry_id
            ),
            new_policy_version="1.1.0",
            summary=(
                "Updated expense validation "
                "policy after incident review."
            ),
            approved_by="Finance Controls",
        )
    )

    assert (
        specification_update.entry_type
        == LedgerEntryType.SPECIFICATION_UPDATE
    )

    # ------------------------------------------------------------
    # STEP 5
    # New request after re-enablement.
    # ------------------------------------------------------------

    new_decision = (
        await governance.authorize(
            action="approve_expense",
            context=ctx(
                amount=150,
            ),
        )
    )

    assert (
        new_decision.outcome
        is Outcome.PROCEED
    )

    assert new_decision.allowed is True

    assert (
        new_decision.suspended is False
    )


# ================================================================
# LEDGER CAUSALITY TESTS
# ================================================================


@pytest.mark.asyncio
async def test_allowed_request_has_expected_ledger_sequence(
    governance,
):
    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=150,
        ),
    )

    assert (
        entry_types_for_decision(
            governance,
            decision.decision_id,
        )
        == [
            LedgerEntryType.MONITOR,
            LedgerEntryType.PRE_NODE,
            LedgerEntryType.VERIFICATION,
        ]
    )


@pytest.mark.asyncio
async def test_pre_node_denial_has_expected_ledger_sequence(
    governance,
):
    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            confidence=0.51,
            amount=150,
        ),
    )

    assert (
        decision.outcome
        is Outcome.HUMAN_QUEUE
    )

    assert (
        entry_types_for_decision(
            governance,
            decision.decision_id,
        )
        == [
            LedgerEntryType.MONITOR,
            LedgerEntryType.PRE_NODE,
            LedgerEntryType.VERIFICATION,
        ]
    )


@pytest.mark.asyncio
async def test_invariant_violation_has_expected_ledger_sequence(
    governance,
):
    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    assert (
        decision.outcome
        is Outcome.SUSPENDED
    )

    assert (
        entry_types_for_decision(
            governance,
            decision.decision_id,
        )
        == [
            LedgerEntryType.MONITOR,
            LedgerEntryType.PRE_NODE,
            LedgerEntryType.VERIFICATION,
            LedgerEntryType.TERMINAL,
        ]
    )


@pytest.mark.asyncio
async def test_suspended_request_has_monitor_only_sequence(
    governance,
):
    first = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    second = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=150,
        ),
    )

    assert first.suspended is True
    assert second.suspended is True

    assert (
        entry_types_for_decision(
            governance,
            second.decision_id,
        )
        == [
            LedgerEntryType.MONITOR,
        ]
    )


# ================================================================
# CAUSED_BY LINK TESTS
# ================================================================


@pytest.mark.asyncio
async def test_terminal_is_caused_by_failed_verification(
    governance,
):
    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    decision_entries = (
        entries_for_decision(
            governance,
            decision.decision_id,
        )
    )

    verification = next(
        entry
        for entry in decision_entries
        if (
            entry.entry_type
            == LedgerEntryType.VERIFICATION
            and entry.payload.get("result")
            == "INSUFFICIENT"
        )
    )

    terminal = next(
        entry
        for entry in decision_entries
        if entry.entry_type
        == LedgerEntryType.TERMINAL
    )

    assert (
        terminal.caused_by
        == verification.entry_id
    )


@pytest.mark.asyncio
async def test_reenable_transition_is_caused_by_terminal(
    governance,
):
    decision = await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    terminal_id = (
        governance.active_suspension()
    )

    governance.reenable(
        authority=finance_authority(),
        authorised_by="j.doe",
        evidence=reenable_evidence(),
    )

    transition = next(
        entry
        for entry in entries(governance)
        if (
            entry.entry_type
            == LedgerEntryType.HUMAN_AUTHORISED_TRANSITION
        )
    )

    assert (
        transition.caused_by
        == terminal_id
    )


@pytest.mark.asyncio
async def test_reenablement_is_caused_by_human_transition(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    governance.reenable(
        authority=finance_authority(),
        authorised_by="j.doe",
        evidence=reenable_evidence(),
    )

    transition = next(
        entry
        for entry in entries(governance)
        if (
            entry.entry_type
            == LedgerEntryType.HUMAN_AUTHORISED_TRANSITION
        )
    )

    reenable_entry = next(
        entry
        for entry in entries(governance)
        if (
            entry.entry_type
            == LedgerEntryType.RE_ENABLEMENT
        )
    )

    assert (
        reenable_entry.caused_by
        == transition.entry_id
    )


@pytest.mark.asyncio
async def test_specification_update_is_caused_by_verification(
    governance,
):
    await governance.authorize(
        action="approve_expense",
        context=ctx(
            amount=5000,
        ),
    )

    insufficient = (
        get_insufficient_verification(
            governance
        )
    )

    update = (
        governance.record_specification_update(
            verification_entry_id=(
                insufficient.entry_id
            ),
            new_policy_version="1.1.0",
            summary="Updated policy",
            approved_by="Finance Controls",
        )
    )

    assert (
        update.caused_by
        == insufficient.entry_id
    )