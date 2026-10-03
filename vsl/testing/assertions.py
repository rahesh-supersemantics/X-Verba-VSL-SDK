"""Readable assertions about governance decisions and ledger evidence.

These are plain functions that raise ``AssertionError`` with a specific
message; they do not need pytest. They encode only what the VSL
Governance Specification and the SDK already guarantee, and never infer
outcomes from free-text reasons: evidence is read from the ledger.
"""

from __future__ import annotations

from typing import Any

from vsl_core.ledger import LedgerEntryType, VerificationResult

from vsl.outcome import Outcome

from .ledger import entries_for_decision, ledger_entries

_MONITOR = LedgerEntryType.MONITOR
_PRE_NODE = LedgerEntryType.PRE_NODE
_VERIFICATION = LedgerEntryType.VERIFICATION
_TERMINAL = LedgerEntryType.TERMINAL


def _fail(message: str) -> None:
    raise AssertionError(message)


# ---------------------------------------------------------------------
# DECISIONS
# ---------------------------------------------------------------------


def assert_proceeded(decision: Any) -> None:
    if decision.outcome is not Outcome.PROCEED:
        _fail(f"expected PROCEED, got {decision.outcome!r}: {decision.reason}")

    if not (
        decision.allowed
        and not decision.requires_human_review
        and not decision.suspended
    ):
        _fail(f"PROCEED decision has inconsistent flags: {decision}")


def assert_human_queued(decision: Any) -> None:
    if decision.outcome is not Outcome.HUMAN_QUEUE:
        _fail(
            f"expected HUMAN_QUEUE, got {decision.outcome!r}: "
            f"{decision.reason}"
        )

    if decision.allowed or decision.suspended:
        _fail(f"HUMAN_QUEUE must not allow or suspend: {decision}")

    if not decision.requires_human_review:
        _fail("HUMAN_QUEUE must require human review")


def assert_suspended(decision: Any, *, terminal_state: str | None = None) -> None:
    if decision.outcome is not Outcome.SUSPENDED:
        _fail(f"expected SUSPENDED, got {decision.outcome!r}")

    if decision.allowed or not decision.suspended:
        _fail(f"SUSPENDED decision has inconsistent flags: {decision}")

    if terminal_state is not None and decision.terminal_state != terminal_state:
        _fail(
            f"expected terminal state {terminal_state!r}, "
            f"got {decision.terminal_state!r}"
        )


# ---------------------------------------------------------------------
# LEDGER EVIDENCE FOR ONE DECISION
# ---------------------------------------------------------------------


def _check_chain(entries: list[Any], expected: list[LedgerEntryType]) -> None:
    actual = [entry.entry_type for entry in entries]

    if actual != expected:
        _fail(
            "ledger evidence for the decision is "
            f"{[t.value for t in actual]}, expected "
            f"{[t.value for t in expected]}"
        )

    for earlier, later in zip(entries, entries[1:]):
        if later.caused_by != earlier.entry_id:
            _fail(
                f"{later.entry_type.value} entry {later.entry_id} has "
                f"caused_by={later.caused_by!r}; expected "
                f"{earlier.entry_id!r} ({earlier.entry_type.value})"
            )


def assert_decision_evidence(governance: Any, decision: Any) -> list[Any]:
    """The ledger holds exactly the evidence the Spec requires for this
    decision, correctly linked by ``caused_by``. Returns the entries.

    PROCEED        MONITOR -> PRE_NODE -> VERIFICATION(SUFFICIENT)
    HUMAN_QUEUE    MONITOR -> PRE_NODE(denied) -> VERIFICATION(SUFFICIENT)
    SUSPENDED      fresh invariant violation:
                   MONITOR -> PRE_NODE -> VERIFICATION(INSUFFICIENT)
                   -> TERMINAL
                   already suspended:
                   MONITOR only, carrying ``suspended_by``
    """

    entries = entries_for_decision(governance, decision.decision_id)

    if not entries:
        _fail(f"no ledger entries for decision {decision.decision_id}")

    if decision.outcome is Outcome.SUSPENDED and len(entries) == 1:
        monitor = entries[0]

        if monitor.entry_type != _MONITOR or "suspended_by" not in monitor.payload:
            _fail(
                "a request blocked by an existing suspension must leave a "
                "single MONITOR entry carrying suspended_by"
            )

        return entries

    if decision.outcome is Outcome.PROCEED:
        _check_chain(entries, [_MONITOR, _PRE_NODE, _VERIFICATION])
        _expect_verification(entries[-1], VerificationResult.SUFFICIENT)

        if entries[1].payload.get("denied"):
            _fail("PROCEED decision has a denied PRE_NODE entry")

    elif decision.outcome is Outcome.HUMAN_QUEUE:
        _check_chain(entries, [_MONITOR, _PRE_NODE, _VERIFICATION])
        _expect_verification(entries[-1], VerificationResult.SUFFICIENT)

        if not entries[1].payload.get("denied"):
            _fail("HUMAN_QUEUE decision must have a denied PRE_NODE entry")

    else:
        _check_chain(entries, [_MONITOR, _PRE_NODE, _VERIFICATION, _TERMINAL])
        _expect_verification(entries[2], VerificationResult.INSUFFICIENT)

        terminal = entries[-1].payload.get("terminal_state")

        if decision.terminal_state is not None and terminal != decision.terminal_state:
            _fail(
                f"TERMINAL entry names {terminal!r} but the decision names "
                f"{decision.terminal_state!r}"
            )

    return entries


def _expect_verification(entry: Any, result: VerificationResult) -> None:
    actual = entry.payload.get("result")

    if actual != result.value:
        _fail(f"expected VERIFICATION {result.value}, got {actual}")


def assert_insufficient_evidence(governance: Any, decision: Any) -> None:
    """Invariant-violation evidence: INSUFFICIENT verification, then a
    TERMINAL entry caused by it."""

    assert_suspended(decision)

    entries = assert_decision_evidence(governance, decision)

    if len(entries) != 4:
        _fail("decision was blocked by an earlier suspension, not by a violation")


# ---------------------------------------------------------------------
# SUSPENSION STATE
# ---------------------------------------------------------------------


def assert_suspension_active(governance: Any) -> str:
    entry_id = governance.active_suspension()

    if entry_id is None:
        _fail("expected an active suspension, found none")

    return entry_id


def assert_not_suspended(governance: Any) -> None:
    entry_id = governance.active_suspension()

    if entry_id is not None:
        _fail(f"unexpected active suspension caused by {entry_id}")


# ---------------------------------------------------------------------
# WHOLE-LEDGER CHECKS (delegating to the SDK, never re-implementing it)
# ---------------------------------------------------------------------


def assert_integrity_ok(governance: Any) -> None:
    if not governance.verify_integrity():
        _fail("ledger integrity verification failed")


def assert_integrity_failed(governance: Any) -> None:
    if governance.verify_integrity():
        _fail("expected integrity verification to fail, but it passed")


def assert_causal_chain_valid(governance: Any) -> None:
    report = governance.validate_caused_by()

    if not report.valid:
        details = "; ".join(f"{i.entry_id}: [{i.code}] {i.detail}" for i in report.issues)
        _fail(f"caused_by chain is invalid: {details}")


def assert_audit_passes(
    governance: Any, *, max_monitor_gap_seconds: float | None = None
) -> None:
    report = governance.audit(max_monitor_gap_seconds=max_monitor_gap_seconds)

    if not report.all_passed:
        _fail(f"audit checks failed: {sorted(report.checks_failed)}")

    assert_causal_chain_valid(governance)


def assert_ledger_grew_by(governance: Any, before: int, expected: int) -> None:
    after = len(ledger_entries(governance))

    if after - before != expected:
        _fail(f"ledger grew by {after - before} entries; expected {expected}")
