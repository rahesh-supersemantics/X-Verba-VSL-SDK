from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from vsl_core.ledger import (
    LedgerEntryType,
    VerificationResult,
)


# ====================================================================
# PHASE 2.5
# caused_by VALIDATION
#
# VSL-Core's audit() matches causally whenever an entry sets
# caused_by, and silently falls back to a "same identity, later
# timestamp" heuristic when it does not. The Spec warns that the
# fallback "can report a false pass", so a passing audit() is only
# trustworthy when every entry that should carry caused_by does, and
# when each one points at the right kind of entry.
#
# This module checks exactly the causal chain the Spec defines
# (Spec section 8 / guide section 15.2). It adds no governance
# semantics of its own.
# ====================================================================


# Entry type -> the entry type(s) its caused_by must reference.
# MONITOR is the root of every chain and has no required cause.
EXPECTED_CAUSE_TYPES: dict[
    LedgerEntryType,
    tuple[LedgerEntryType, ...],
] = {
    LedgerEntryType.PRE_NODE: (
        LedgerEntryType.MONITOR,
    ),
    LedgerEntryType.VERIFICATION: (
        LedgerEntryType.PRE_NODE,
    ),
    LedgerEntryType.TERMINAL: (
        LedgerEntryType.VERIFICATION,
    ),
    LedgerEntryType.SPECIFICATION_UPDATE: (
        LedgerEntryType.VERIFICATION,
    ),
    LedgerEntryType.HUMAN_AUTHORISED_TRANSITION: (
        LedgerEntryType.TERMINAL,
    ),
    LedgerEntryType.RE_ENABLEMENT: (
        LedgerEntryType.HUMAN_AUTHORISED_TRANSITION,
    ),
}

# Entries that only make sense as a response to a governance FAILURE:
# their cause must be an INSUFFICIENT verification.
_REQUIRES_INSUFFICIENT_CAUSE = frozenset(
    {
        LedgerEntryType.TERMINAL,
        LedgerEntryType.SPECIFICATION_UPDATE,
    }
)


# Issue codes.
MISSING_CAUSED_BY = "missing_caused_by"
UNKNOWN_CAUSE = "unknown_cause"
WRONG_CAUSE_TYPE = "wrong_cause_type"
CAUSE_NOT_INSUFFICIENT = "cause_not_insufficient"
CAUSE_NOT_EARLIER = "cause_not_earlier"


@dataclass(frozen=True)
class CausedByIssue:
    """One causal-link problem on one ledger entry."""

    entry_id: str
    entry_type: LedgerEntryType
    code: str
    detail: str


@dataclass(frozen=True)
class CausedByReport:
    """Result of validating caused_by across a ledger."""

    entries_checked: int
    issues: tuple[CausedByIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def violations(self) -> dict[str, list[str]]:
        """Issue code -> entry IDs, mirroring LedgerAuditReport."""

        grouped: dict[str, list[str]] = {}

        for issue in self.issues:
            grouped.setdefault(
                issue.code,
                [],
            ).append(issue.entry_id)

        return grouped


def validate_caused_by(
    entries: Iterable[Any],
) -> CausedByReport:
    """
    Validate the caused_by chain of the given ledger entries.

    For every entry whose type has a required cause:

    - caused_by must be set
    - it must reference an entry that exists in the ledger
    - the referenced entry must be of the type the Spec prescribes
    - TERMINAL and SPECIFICATION_UPDATE must reference an
      INSUFFICIENT verification
    - the cause must come earlier in the chain than its effect

    A dangling caused_by is reported on any entry type, including
    MONITOR. Read-only: nothing is written or repaired.
    """

    all_entries = list(entries)

    by_id = {
        entry.entry_id: entry
        for entry in all_entries
    }

    issues: list[CausedByIssue] = []

    def report(
        entry: Any,
        code: str,
        detail: str,
    ) -> None:
        issues.append(
            CausedByIssue(
                entry_id=entry.entry_id,
                entry_type=entry.entry_type,
                code=code,
                detail=detail,
            )
        )

    for entry in all_entries:

        expected_types = EXPECTED_CAUSE_TYPES.get(
            entry.entry_type
        )

        # -------------------------------------------------------------
        # Required cause is missing.
        # -------------------------------------------------------------

        if not entry.caused_by:

            if expected_types is not None:
                report(
                    entry,
                    MISSING_CAUSED_BY,
                    f"{entry.entry_type.value} must set "
                    "caused_by; audit() would fall back to "
                    "the timestamp heuristic.",
                )

            continue

        # -------------------------------------------------------------
        # caused_by must resolve to a real entry.
        # -------------------------------------------------------------

        cause = by_id.get(entry.caused_by)

        if cause is None:
            report(
                entry,
                UNKNOWN_CAUSE,
                f"caused_by {entry.caused_by!r} does not "
                "match any ledger entry.",
            )
            continue

        if expected_types is None:
            continue

        # -------------------------------------------------------------
        # The cause must be the right kind of entry.
        # -------------------------------------------------------------

        if cause.entry_type not in expected_types:
            report(
                entry,
                WRONG_CAUSE_TYPE,
                f"{entry.entry_type.value} must be caused by "
                + " or ".join(
                    t.value for t in expected_types
                )
                + f", not {cause.entry_type.value}.",
            )
            continue

        if (
            entry.entry_type
            in _REQUIRES_INSUFFICIENT_CAUSE
            and cause.payload.get("result")
            != VerificationResult.INSUFFICIENT.value
        ):
            report(
                entry,
                CAUSE_NOT_INSUFFICIENT,
                f"{entry.entry_type.value} must be caused by "
                "an INSUFFICIENT verification.",
            )

        # -------------------------------------------------------------
        # A cause must precede its effect.
        # -------------------------------------------------------------

        if cause.sequence >= entry.sequence:
            report(
                entry,
                CAUSE_NOT_EARLIER,
                f"cause {cause.entry_id} (sequence "
                f"{cause.sequence}) does not precede this "
                f"entry (sequence {entry.sequence}).",
            )

    return CausedByReport(
        entries_checked=len(all_entries),
        issues=tuple(issues),
    )
