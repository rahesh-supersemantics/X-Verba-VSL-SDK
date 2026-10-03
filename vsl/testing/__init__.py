"""Reusable governance testing infrastructure for X-Verba projects and
framework adapters. See the README section "Testing framework".

Nothing here imports a framework adapter, and nothing registers itself
with pytest automatically.
"""

from .assertions import (
    assert_audit_passes,
    assert_causal_chain_valid,
    assert_decision_evidence,
    assert_human_queued,
    assert_insufficient_evidence,
    assert_integrity_failed,
    assert_integrity_ok,
    assert_ledger_grew_by,
    assert_not_suspended,
    assert_proceeded,
    assert_suspended,
    assert_suspension_active,
)
from .harness import GovernedHarness, build_harness
from .ledger import (
    break_link,
    drop_entry,
    entries_for_decision,
    latest_decision_id,
    ledger_entries,
    tamper_entry_hash,
    tamper_payload,
    truncate,
)
from .lifecycle import resolve_suspension, suspend
from .policies import (
    EXPENSE_ACTION,
    EXPENSE_CAP,
    EXPENSE_POLICY_YAML,
    EXPENSE_TERMINAL_STATE,
    low_confidence_context,
    ok_context,
    over_cap_context,
    write_policy,
)
from .policy_checks import (
    error_messages,
    load_policy_text,
    policy_error,
    replace_in_policy,
)
from .side_effect import AsyncSideEffect, SideEffect

__all__ = [
    "AsyncSideEffect",
    "EXPENSE_ACTION",
    "EXPENSE_CAP",
    "EXPENSE_POLICY_YAML",
    "EXPENSE_TERMINAL_STATE",
    "GovernedHarness",
    "SideEffect",
    "assert_audit_passes",
    "assert_causal_chain_valid",
    "assert_decision_evidence",
    "assert_human_queued",
    "assert_insufficient_evidence",
    "assert_integrity_failed",
    "assert_integrity_ok",
    "assert_ledger_grew_by",
    "assert_not_suspended",
    "assert_proceeded",
    "assert_suspended",
    "assert_suspension_active",
    "break_link",
    "build_harness",
    "drop_entry",
    "entries_for_decision",
    "error_messages",
    "latest_decision_id",
    "ledger_entries",
    "load_policy_text",
    "low_confidence_context",
    "ok_context",
    "over_cap_context",
    "policy_error",
    "replace_in_policy",
    "resolve_suspension",
    "suspend",
    "tamper_entry_hash",
    "tamper_payload",
    "truncate",
    "write_policy",
]
