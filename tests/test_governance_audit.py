"""
Phase 2.5 — Ledger Integrity & Governance Audit.

verify_integrity(): were records edited?
audit():            do the records describe a governed process?
validate_caused_by(): is the causal chain itself well formed?
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from vsl import (
    CausedByReport,
    Governance,
    validate_caused_by,
)
from vsl.audit import (
    CAUSE_NOT_EARLIER,
    CAUSE_NOT_INSUFFICIENT,
    MISSING_CAUSED_BY,
    UNKNOWN_CAUSE,
    WRONG_CAUSE_TYPE,
)

from vsl_core.governance import GovernanceAuthority
from vsl_core.identity import Evidence
from vsl_core.ledger import (
    LedgerAuditReport,
    LedgerEntryType,
    VerificationResult,
)


AGENT = "expense-agent"

FIVE_CHECKS = (
    "no_monitoring_gaps",
    "drift_flagged_monitor_has_pre_node",
    "pre_node_has_verification",
    "insufficient_verification_has_specification_update",
    "terminal_has_human_authorised_transition",
)

CHECK_GAPS = FIVE_CHECKS[0]
CHECK_DRIFT = FIVE_CHECKS[1]
CHECK_PRE_NODE = FIVE_CHECKS[2]
CHECK_SPEC_UPDATE = FIVE_CHECKS[3]
CHECK_TERMINAL = FIVE_CHECKS[4]


# ================================================================
# HELPERS
# ================================================================


def ok_context() -> dict:
    return {"confidence": 0.95, "amount": 150, "currency": "NZD"}


def low_confidence_context() -> dict:
    return {"confidence": 0.51, "amount": 150, "currency": "NZD"}


def over_cap_context() -> dict:
    return {"confidence": 0.95, "amount": 5000, "currency": "NZD"}


def entries(governance: Governance) -> list:
    return list(governance.ledger.store.all_entries())


def entries_of(governance: Governance, entry_type) -> list:
    return [
        e for e in entries(governance)
        if e.entry_type == entry_type
    ]


def ledger_path(governance: Governance):
    return governance.ledger.store.path


def read_lines(governance: Governance) -> list[str]:
    return ledger_path(governance).read_text().splitlines()


def write_lines(governance: Governance, lines: list[str]) -> None:
    ledger_path(governance).write_text(
        "\n".join(lines) + "\n"
    )


def write_entry(governance: Governance, entry_type, **kwargs):
    """Write a ledger entry directly, bypassing Governance."""

    return governance.ledger.write(
        entry_type,
        identity_key=AGENT,
        instance_id="instance-under-test",
        **kwargs,
    )


def resolve(governance: Governance) -> None:
    """Human re-enablement plus specification update."""

    insufficient = next(
        e for e in entries_of(
            governance, LedgerEntryType.VERIFICATION
        )
        if e.payload["result"] == "INSUFFICIENT"
    )

    governance.reenable(
        authority=GovernanceAuthority(
            name="Finance Controls",
            escalation_contact="controls@example.com",
        ),
        authorised_by="j.doe",
        evidence=Evidence(
            root_cause_analysis="OCR misread the amount.",
            specification_update_proposal=(
                "Add an OCR-confidence PreNode."
            ),
        ),
    )

    governance.record_specification_update(
        verification_entry_id=insufficient.entry_id,
        new_policy_version="1.1.0",
        summary="Added OCR-confidence PreNode",
        approved_by="Finance Controls",
    )


def fake_entry(entry_id, entry_type, sequence, caused_by=None, payload=None):
    return SimpleNamespace(
        entry_id=entry_id,
        entry_type=entry_type,
        sequence=sequence,
        caused_by=caused_by,
        payload=payload or {},
    )


# ================================================================
# verify_integrity() — SUCCESS
# ================================================================


def test_verify_integrity_true_for_empty_ledger(governance):
    assert governance.verify_integrity() is True


@pytest.mark.asyncio
async def test_verify_integrity_true_for_clean_traffic(governance):
    await governance.authorize("approve_expense", ok_context())
    await governance.authorize("approve_expense", low_confidence_context())
    await governance.authorize("approve_expense", over_cap_context())

    assert governance.verify_integrity() is True


@pytest.mark.asyncio
async def test_verify_integrity_true_after_full_lifecycle(governance):
    await governance.authorize("approve_expense", over_cap_context())
    resolve(governance)

    assert governance.verify_integrity() is True


@pytest.mark.asyncio
async def test_verify_integrity_matches_vsl_core(governance):
    await governance.authorize("approve_expense", ok_context())

    assert (
        governance.verify_integrity()
        is governance.ledger.verify_integrity()
    )


# ================================================================
# verify_integrity() — FAILURE
# ================================================================


@pytest.mark.asyncio
async def test_verify_integrity_detects_edited_payload(governance):
    await governance.authorize("approve_expense", ok_context())

    lines = read_lines(governance)
    first = json.loads(lines[0])
    first["payload"]["amount"] = 1
    lines[0] = json.dumps(first)
    write_lines(governance, lines)

    assert governance.verify_integrity() is False


@pytest.mark.asyncio
async def test_verify_integrity_detects_removed_middle_entry(governance):
    await governance.authorize("approve_expense", ok_context())

    lines = read_lines(governance)
    del lines[1]
    write_lines(governance, lines)

    # Sequence gap and broken prev_hash link.
    assert governance.verify_integrity() is False


@pytest.mark.asyncio
async def test_verify_integrity_detects_reordered_entries(governance):
    await governance.authorize("approve_expense", ok_context())

    lines = read_lines(governance)
    lines[1], lines[2] = lines[2], lines[1]
    write_lines(governance, lines)

    assert governance.verify_integrity() is False


@pytest.mark.asyncio
async def test_verify_integrity_never_raises_on_tampered_ledger(governance):
    await governance.authorize("approve_expense", ok_context())

    lines = read_lines(governance)
    last = json.loads(lines[-1])
    last["payload"]["outcome"] = "forged"
    lines[-1] = json.dumps(last)
    write_lines(governance, lines)

    assert governance.verify_integrity() is False


@pytest.mark.asyncio
async def test_tampering_is_not_an_audit_failure(governance):
    """Spec: a tampered payload gives integrity=False while all five
    checks still pass. Both must be run."""

    await governance.authorize("approve_expense", ok_context())

    lines = read_lines(governance)
    first = json.loads(lines[0])
    first["payload"]["amount"] = 1
    lines[0] = json.dumps(first)
    write_lines(governance, lines)

    assert governance.verify_integrity() is False
    assert governance.audit().all_passed


@pytest.mark.asyncio
async def test_truncation_is_not_detected_but_checkpoint_reveals_it(
    governance,
):
    """Known constraint 5: a shortened chain is still a valid chain."""

    await governance.authorize("approve_expense", ok_context())

    anchored = governance.ledger.current_checkpoint()

    write_lines(governance, read_lines(governance)[:-1])

    assert governance.verify_integrity() is True
    assert (
        governance.ledger.current_checkpoint().sequence
        < anchored.sequence
    )


# ================================================================
# audit() — SHAPE
# ================================================================


def test_audit_returns_vsl_core_report(governance):
    report = governance.audit()

    assert isinstance(report, LedgerAuditReport)


def test_audit_empty_ledger_passes_all_five_checks(governance):
    report = governance.audit(max_monitor_gap_seconds=300)

    assert report.all_passed
    assert report.checks_passed == FIVE_CHECKS
    assert report.checks_failed == ()
    assert report.violations == {}


# ================================================================
# audit() — SUCCESS
# ================================================================


@pytest.mark.asyncio
async def test_audit_passes_for_proceed(governance):
    await governance.authorize("approve_expense", ok_context())

    report = governance.audit(max_monitor_gap_seconds=300)

    assert report.all_passed
    assert report.checks_passed == FIVE_CHECKS


@pytest.mark.asyncio
async def test_audit_passes_for_pre_node_denial(governance):
    """An ordinary denial is SUFFICIENT, never INSUFFICIENT."""

    await governance.authorize("approve_expense", low_confidence_context())

    report = governance.audit(max_monitor_gap_seconds=300)

    assert report.all_passed


@pytest.mark.asyncio
async def test_audit_passes_after_reenable_and_specification_update(
    governance,
):
    await governance.authorize("approve_expense", over_cap_context())
    resolve(governance)

    report = governance.audit(max_monitor_gap_seconds=300)

    assert report.all_passed
    assert report.checks_passed == FIVE_CHECKS


@pytest.mark.asyncio
async def test_audit_passes_with_requests_made_while_suspended(governance):
    """Suspended requests record MONITOR with drift_detected=False,
    so audit check 2 is unaffected, before and after re-enablement."""

    await governance.authorize("approve_expense", over_cap_context())
    await governance.authorize("approve_expense", ok_context())
    await governance.authorize("approve_expense", ok_context())

    assert CHECK_DRIFT not in governance.audit().checks_failed

    resolve(governance)

    assert governance.audit().all_passed


# ================================================================
# audit() — FAILURE: CHECKS 4 AND 5
# ================================================================


@pytest.mark.asyncio
async def test_audit_fails_checks_4_and_5_while_suspended(governance):
    await governance.authorize("approve_expense", over_cap_context())

    report = governance.audit()

    assert not report.all_passed
    assert set(report.checks_failed) == {
        CHECK_SPEC_UPDATE,
        CHECK_TERMINAL,
    }
    assert set(report.checks_passed) == {
        CHECK_GAPS,
        CHECK_DRIFT,
        CHECK_PRE_NODE,
    }


@pytest.mark.asyncio
async def test_audit_violations_name_the_offending_entries(governance):
    await governance.authorize("approve_expense", over_cap_context())

    insufficient = next(
        e for e in entries_of(
            governance, LedgerEntryType.VERIFICATION
        )
        if e.payload["result"] == "INSUFFICIENT"
    )
    terminal = entries_of(governance, LedgerEntryType.TERMINAL)[0]

    report = governance.audit()

    assert report.violations[CHECK_SPEC_UPDATE] == [
        insufficient.entry_id
    ]
    assert report.violations[CHECK_TERMINAL] == [
        terminal.entry_id
    ]


@pytest.mark.asyncio
async def test_audit_check_5_passes_after_reenable_but_check_4_still_fails(
    governance,
):
    await governance.authorize("approve_expense", over_cap_context())

    governance.reenable(
        authority=GovernanceAuthority(
            name="Finance Controls",
            escalation_contact="controls@example.com",
        ),
        authorised_by="j.doe",
        evidence=Evidence(
            root_cause_analysis="OCR misread the amount.",
            specification_update_proposal="Add an OCR PreNode.",
        ),
    )

    report = governance.audit()

    assert report.checks_failed == (CHECK_SPEC_UPDATE,)
    assert CHECK_TERMINAL in report.checks_passed


@pytest.mark.asyncio
async def test_audit_check_4_passes_with_spec_update_but_check_5_fails(
    governance,
):
    await governance.authorize("approve_expense", over_cap_context())

    insufficient = next(
        e for e in entries_of(
            governance, LedgerEntryType.VERIFICATION
        )
        if e.payload["result"] == "INSUFFICIENT"
    )

    governance.record_specification_update(
        verification_entry_id=insufficient.entry_id,
        new_policy_version="1.1.0",
        summary="Tighten the cap rule",
        approved_by="Finance Controls",
    )

    report = governance.audit()

    assert report.checks_failed == (CHECK_TERMINAL,)
    assert CHECK_SPEC_UPDATE in report.checks_passed


# ================================================================
# audit() — FAILURE: CHECKS 1, 2, 3
# ================================================================


@pytest.mark.asyncio
async def test_audit_check_1_detects_monitoring_gap(governance):
    await governance.authorize("approve_expense", ok_context())
    time.sleep(0.05)
    await governance.authorize("approve_expense", ok_context())

    report = governance.audit(max_monitor_gap_seconds=0.01)

    assert CHECK_GAPS in report.checks_failed
    assert len(report.violations[CHECK_GAPS]) == 1


@pytest.mark.asyncio
async def test_audit_check_1_passes_within_threshold(governance):
    await governance.authorize("approve_expense", ok_context())
    time.sleep(0.05)
    await governance.authorize("approve_expense", ok_context())

    report = governance.audit(max_monitor_gap_seconds=300)

    assert CHECK_GAPS in report.checks_passed


@pytest.mark.asyncio
async def test_audit_check_1_is_skipped_when_threshold_is_none(governance):
    await governance.authorize("approve_expense", ok_context())
    time.sleep(0.05)
    await governance.authorize("approve_expense", ok_context())

    report = governance.audit()

    assert CHECK_GAPS in report.checks_passed


def test_audit_check_2_detects_drift_monitor_without_pre_node(governance):
    monitor = governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        drift_detected=True,
    )

    report = governance.audit()

    assert report.checks_failed == (CHECK_DRIFT,)
    assert report.violations[CHECK_DRIFT] == [monitor.entry_id]


def test_audit_check_2_passes_when_pre_node_resolves_drift(governance):
    monitor = governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        drift_detected=True,
    )
    pre_node = write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        caused_by=monitor.entry_id,
        payload={"denied": False},
    )
    governance.ledger.write_verification(
        identity_key=AGENT,
        instance_id="instance-under-test",
        caused_by=pre_node.entry_id,
        result=VerificationResult.SUFFICIENT,
    )

    assert governance.audit().all_passed


def test_audit_check_3_detects_pre_node_without_verification(governance):
    monitor = governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        drift_detected=False,
    )
    pre_node = write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        caused_by=monitor.entry_id,
        payload={"denied": False},
    )

    report = governance.audit()

    assert report.checks_failed == (CHECK_PRE_NODE,)
    assert report.violations[CHECK_PRE_NODE] == [pre_node.entry_id]


# ================================================================
# validate_caused_by() — SUCCESS
# ================================================================


def test_caused_by_valid_for_empty_ledger(governance):
    report = governance.validate_caused_by()

    assert isinstance(report, CausedByReport)
    assert report.valid
    assert report.entries_checked == 0
    assert report.issues == ()


@pytest.mark.asyncio
async def test_caused_by_valid_for_every_outcome(governance):
    await governance.authorize("approve_expense", ok_context())
    await governance.authorize("approve_expense", low_confidence_context())
    await governance.authorize("approve_expense", over_cap_context())
    await governance.authorize("approve_expense", ok_context())  # suspended

    report = governance.validate_caused_by()

    assert report.valid, report.issues
    assert report.entries_checked == len(entries(governance))


@pytest.mark.asyncio
async def test_caused_by_valid_after_full_lifecycle(governance):
    await governance.authorize("approve_expense", over_cap_context())
    resolve(governance)
    await governance.authorize("approve_expense", ok_context())

    report = governance.validate_caused_by()

    assert report.valid, report.issues
    assert report.violations == {}


@pytest.mark.asyncio
async def test_validate_caused_by_is_read_only(governance):
    await governance.authorize("approve_expense", over_cap_context())

    before = len(entries(governance))

    governance.validate_caused_by()
    governance.audit()
    governance.verify_integrity()

    assert len(entries(governance)) == before


# ================================================================
# validate_caused_by() — FAILURE (real ledger entries)
# ================================================================


def test_caused_by_detects_missing_caused_by(governance):
    write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        payload={"denied": False},
    )

    report = governance.validate_caused_by()

    assert not report.valid
    assert [i.code for i in report.issues] == [MISSING_CAUSED_BY]
    assert report.issues[0].entry_type == LedgerEntryType.PRE_NODE


def test_caused_by_detects_dangling_reference(governance):
    write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        caused_by="no-such-entry",
        payload={"denied": False},
    )

    report = governance.validate_caused_by()

    assert [i.code for i in report.issues] == [UNKNOWN_CAUSE]


def test_caused_by_detects_dangling_reference_on_monitor(governance):
    governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        caused_by="no-such-entry",
        drift_detected=False,
    )

    report = governance.validate_caused_by()

    assert [i.code for i in report.issues] == [UNKNOWN_CAUSE]


def test_caused_by_monitor_without_cause_is_valid(governance):
    governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        drift_detected=False,
    )

    assert governance.validate_caused_by().valid


def test_caused_by_detects_wrong_cause_type(governance):
    monitor = governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        drift_detected=False,
    )

    # A VERIFICATION must be caused by a PRE_NODE, not a MONITOR.
    governance.ledger.write_verification(
        identity_key=AGENT,
        instance_id="instance-under-test",
        caused_by=monitor.entry_id,
        result=VerificationResult.SUFFICIENT,
    )

    report = governance.validate_caused_by()

    assert [i.code for i in report.issues] == [WRONG_CAUSE_TYPE]
    assert report.issues[0].entry_type == LedgerEntryType.VERIFICATION


def test_caused_by_detects_terminal_caused_by_sufficient_verification(
    governance,
):
    monitor = governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        drift_detected=False,
    )
    pre_node = write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        caused_by=monitor.entry_id,
        payload={"denied": False},
    )
    sufficient = governance.ledger.write_verification(
        identity_key=AGENT,
        instance_id="instance-under-test",
        caused_by=pre_node.entry_id,
        result=VerificationResult.SUFFICIENT,
    )
    write_entry(
        governance,
        LedgerEntryType.TERMINAL,
        caused_by=sufficient.entry_id,
        payload={"terminal_state": "expense-approvals-suspended"},
    )

    report = governance.validate_caused_by()

    assert [i.code for i in report.issues] == [CAUSE_NOT_INSUFFICIENT]
    assert report.issues[0].entry_type == LedgerEntryType.TERMINAL


def test_caused_by_detects_spec_update_caused_by_sufficient_verification(
    governance,
):
    monitor = governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        drift_detected=False,
    )
    pre_node = write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        caused_by=monitor.entry_id,
        payload={"denied": False},
    )
    sufficient = governance.ledger.write_verification(
        identity_key=AGENT,
        instance_id="instance-under-test",
        caused_by=pre_node.entry_id,
        result=VerificationResult.SUFFICIENT,
    )
    write_entry(
        governance,
        LedgerEntryType.SPECIFICATION_UPDATE,
        caused_by=sufficient.entry_id,
        payload={"policy_version": "1.1.0"},
    )

    report = governance.validate_caused_by()

    assert [i.code for i in report.issues] == [CAUSE_NOT_INSUFFICIENT]


@pytest.mark.asyncio
async def test_caused_by_detects_wrong_type_on_human_transition_and_re_enablement(
    governance,
):
    await governance.authorize("approve_expense", ok_context())

    monitor = entries_of(governance, LedgerEntryType.MONITOR)[0]

    # Both should chain from TERMINAL / HUMAN_AUTHORISED_TRANSITION.
    write_entry(
        governance,
        LedgerEntryType.HUMAN_AUTHORISED_TRANSITION,
        caused_by=monitor.entry_id,
        payload={},
    )
    write_entry(
        governance,
        LedgerEntryType.RE_ENABLEMENT,
        caused_by=monitor.entry_id,
        payload={},
    )

    report = governance.validate_caused_by()

    assert [i.code for i in report.issues] == [
        WRONG_CAUSE_TYPE,
        WRONG_CAUSE_TYPE,
    ]


def test_caused_by_reports_every_issue_grouped_by_code(governance):
    first = write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        payload={"denied": False},
    )
    second = write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        payload={"denied": False},
    )
    dangling = write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        caused_by="no-such-entry",
        payload={"denied": False},
    )

    report = governance.validate_caused_by()

    assert report.violations == {
        MISSING_CAUSED_BY: [first.entry_id, second.entry_id],
        UNKNOWN_CAUSE: [dangling.entry_id],
    }


# ================================================================
# validate_caused_by() — why it exists
# ================================================================


def test_audit_false_pass_without_caused_by_is_caught_by_validation(
    governance,
):
    """Spec section 8: without caused_by, audit() falls back to a
    'same identity, later timestamp' heuristic and can report a false
    pass. validate_caused_by() is what exposes it."""

    governance.ledger.write_monitor(
        identity_key=AGENT,
        instance_id="instance-under-test",
        drift_detected=True,
    )
    pre_node = write_entry(
        governance,
        LedgerEntryType.PRE_NODE,
        payload={"denied": False},          # no caused_by
    )
    governance.ledger.write_verification(
        identity_key=AGENT,
        instance_id="instance-under-test",
        caused_by=pre_node.entry_id,
        result=VerificationResult.SUFFICIENT,
    )

    assert governance.audit().all_passed    # heuristic false pass

    report = governance.validate_caused_by()

    assert not report.valid
    assert report.violations == {
        MISSING_CAUSED_BY: [pre_node.entry_id]
    }


# ================================================================
# validate_caused_by() — pure function, fabricated entries
# ================================================================


def test_caused_by_detects_cause_that_does_not_precede_effect():
    cause = fake_entry(
        "b", LedgerEntryType.MONITOR, sequence=1
    )
    effect = fake_entry(
        "a",
        LedgerEntryType.PRE_NODE,
        sequence=0,
        caused_by="b",
    )

    report = validate_caused_by([effect, cause])

    assert [i.code for i in report.issues] == [CAUSE_NOT_EARLIER]
    assert report.issues[0].entry_id == "a"


def test_caused_by_detects_self_reference():
    entry = fake_entry(
        "a",
        LedgerEntryType.VERIFICATION,
        sequence=0,
        caused_by="a",
    )

    report = validate_caused_by([entry])

    codes = [i.code for i in report.issues]

    assert WRONG_CAUSE_TYPE in codes


def test_caused_by_accepts_well_formed_chain():
    monitor = fake_entry("m", LedgerEntryType.MONITOR, 0)
    pre_node = fake_entry(
        "p", LedgerEntryType.PRE_NODE, 1, caused_by="m"
    )
    verification = fake_entry(
        "v",
        LedgerEntryType.VERIFICATION,
        2,
        caused_by="p",
        payload={"result": "INSUFFICIENT"},
    )
    terminal = fake_entry(
        "t", LedgerEntryType.TERMINAL, 3, caused_by="v"
    )
    spec = fake_entry(
        "s",
        LedgerEntryType.SPECIFICATION_UPDATE,
        4,
        caused_by="v",
    )
    transition = fake_entry(
        "h",
        LedgerEntryType.HUMAN_AUTHORISED_TRANSITION,
        5,
        caused_by="t",
    )
    reenable = fake_entry(
        "r",
        LedgerEntryType.RE_ENABLEMENT,
        6,
        caused_by="h",
    )

    report = validate_caused_by(
        [monitor, pre_node, verification, terminal,
         spec, transition, reenable]
    )

    assert report.valid
    assert report.entries_checked == 7
