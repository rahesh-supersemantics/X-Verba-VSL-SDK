"""Lifecycle scenarios: suspend a governed agent, then re-enable it the
way the Specification requires (a named human authority, evidence, and a
specification update caused by the INSUFFICIENT verification)."""

from __future__ import annotations

from typing import Any

from vsl_core.governance import GovernanceAuthority
from vsl_core.identity import Evidence
from vsl_core.ledger import LedgerEntryType

from .assertions import assert_suspended
from .policies import EXPENSE_ACTION, over_cap_context


async def suspend(
    governance: Any,
    *,
    action: str = EXPENSE_ACTION,
    context: dict[str, Any] | None = None,
) -> Any:
    """Drive an invariant violation; returns the SUSPENDED Decision."""

    decision = await governance.authorize(
        action, over_cap_context() if context is None else context
    )

    assert_suspended(decision)

    return decision


def resolve_suspension(
    governance: Any,
    *,
    authority_name: str = "Finance Controls",
    authorised_by: str = "j.doe",
    new_policy_version: str = "1.1.0",
) -> None:
    """Re-enable and record the specification update for the most recent
    INSUFFICIENT verification."""

    insufficient = [
        entry
        for entry in governance.ledger.store.all_entries()
        if entry.entry_type == LedgerEntryType.VERIFICATION
        and entry.payload.get("result") == "INSUFFICIENT"
    ]

    if not insufficient:
        raise AssertionError("no INSUFFICIENT verification to resolve")

    governance.reenable(
        authority=GovernanceAuthority(
            name=authority_name,
            escalation_contact=f"{authority_name.lower().replace(' ', '.')}@example.com",
        ),
        authorised_by=authorised_by,
        evidence=Evidence(
            root_cause_analysis="Test scenario root cause.",
            specification_update_proposal="Test scenario specification update.",
        ),
    )

    governance.record_specification_update(
        verification_entry_id=insufficient[-1].entry_id,
        new_policy_version=new_policy_version,
        summary="Test scenario specification update",
        approved_by=authority_name,
    )
