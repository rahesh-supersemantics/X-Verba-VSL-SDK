"""One-shot ledger inspection: integrity, audit, causal chain,
checkpoint and certification.

A thin composition of calls that already exist:

    integrity    VerbaLedger.verify_integrity()      (VSL-Core)
    audit        VerbaLedger.audit()                 (VSL-Core)
    causal       vsl.audit.validate_caused_by()      (Phase 2.5)
    checkpoint   vsl.checkpoint.verify_checkpoint()  (Phase 4)
    certificate  VerbaLedger.issue_certificate()     (VSL-Core)

Nothing here re-implements any of them. A certificate is only ever the
real VSL-Core VerbaCertificate, and only when every requirement below
holds. Passing a certificate request never "succeeds by default".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from vsl_core.ledger import (
    LedgerAuditReport,
    LedgerCheckpoint,
    VerbaCertificate,
    VerbaLedger,
)

from .audit import CausedByReport, validate_caused_by
from .checkpoint import CheckpointVerification, verify_checkpoint


@dataclass(frozen=True)
class LedgerInspection:
    """Everything learned about a ledger in one pass."""

    entries_checked: int
    integrity_ok: bool
    audit: LedgerAuditReport
    causal: CausedByReport
    max_monitor_gap_seconds: float | None
    checkpoint: CheckpointVerification | None
    certificate: VerbaCertificate | None
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True when nothing failed. For a certification request this
        also covers the certification-only requirements."""

        return not self.failures


def validate_gap(value: Any) -> float | None:
    """None, or a finite, non-negative number of seconds."""

    if value is None:
        return None

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(
            "max_monitor_gap_seconds must be a finite number "
            ">= 0 (or None)."
        )

    return float(value)


def inspect_ledger(
    ledger: VerbaLedger,
    *,
    max_monitor_gap_seconds: float | None = None,
    checkpoint: LedgerCheckpoint | None = None,
    certify: bool = False,
) -> LedgerInspection:
    """Inspect a ledger.

    With certify=True the inspection additionally requires a
    monitoring-gap threshold (otherwise audit check 1 is skipped and a
    certificate would claim continuous monitoring that was never
    measured) and a non-empty ledger (an empty ledger certifies
    nothing). A certificate is issued only if there are no failures.
    """

    gap = validate_gap(max_monitor_gap_seconds)

    entries = list(ledger.store.all_entries())

    integrity_ok = ledger.verify_integrity()

    audit = ledger.audit(max_monitor_gap_seconds=gap)

    causal = validate_caused_by(entries)

    checkpoint_result = (
        verify_checkpoint(ledger, checkpoint)
        if checkpoint is not None
        else None
    )

    failures: list[str] = []

    if not integrity_ok:
        failures.append(
            "integrity: hash chain, link or sequence check failed"
        )

    if not audit.all_passed:
        failures.append(
            "audit: failed " + ", ".join(audit.checks_failed)
        )

    if not causal.valid:
        failures.append(
            f"caused_by: {len(causal.issues)} causal-chain "
            "issue(s)"
        )

    if checkpoint_result is not None and not checkpoint_result.ok:
        failures.append(
            f"checkpoint: {checkpoint_result.code}: "
            f"{checkpoint_result.detail}"
        )

    if certify:
        if gap is None:
            failures.append(
                "certification: a max_monitor_gap_seconds "
                "threshold is required"
            )

        if not entries:
            failures.append(
                "certification: the ledger is empty, so there is "
                "no governance evidence to certify"
            )

    certificate = None

    if certify and not failures:
        certificate = ledger.issue_certificate(
            max_monitor_gap_seconds=gap
        )

        if certificate is None:
            failures.append(
                "certification: VSL-Core declined to issue a "
                "certificate"
            )

    return LedgerInspection(
        entries_checked=len(entries),
        integrity_ok=integrity_ok,
        audit=audit,
        causal=causal,
        max_monitor_gap_seconds=gap,
        checkpoint=checkpoint_result,
        certificate=certificate,
        failures=tuple(failures),
    )
