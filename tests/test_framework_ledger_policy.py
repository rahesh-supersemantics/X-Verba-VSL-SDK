"""
Phase 5 -- ledger-integrity and policy scenarios written with the testing
framework.

These reuse the Phase 2.5-4 implementation (verify_integrity, audit,
validate_caused_by, checkpoints, certificates, YAML loading); nothing here
re-implements ledger or policy semantics.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from vsl import (
    CheckpointVerification,
    Outcome,
    PolicyConfigError,
    PolicySchemaError,
    PolicyValidationError,
    RegistryError,
    SDKError,
    open_jsonl_ledger,
    inspect_ledger,
)
from vsl.testing import (
    EXPENSE_ACTION,
    EXPENSE_POLICY_YAML,
    assert_audit_passes,
    assert_causal_chain_valid,
    assert_integrity_failed,
    assert_integrity_ok,
    break_link,
    build_harness,
    drop_entry,
    error_messages,
    load_policy_text,
    low_confidence_context,
    ok_context,
    over_cap_context,
    policy_error,
    replace_in_policy,
    resolve_suspension,
    suspend,
    tamper_entry_hash,
    tamper_payload,
    truncate,
    write_policy,
)

from governance.policy import build_policy
from vsl import Governance
from vsl.ledger import memory_ledger

GAP = 300


def traffic(harness):
    """Six clean entries: one PROCEED and one HUMAN_QUEUE."""

    asyncio.run(harness.authorize(EXPENSE_ACTION, ok_context()))
    asyncio.run(harness.authorize(EXPENSE_ACTION, low_confidence_context()))


# =====================================================================
# LEDGER: creation, entries, a clean ledger verifies
# =====================================================================


def test_a_new_ledger_file_is_created_on_first_write(tmp_path):
    harness = build_harness(tmp_path)

    assert not harness.ledger_path.exists() or harness.ledger_path.read_text() == ""

    traffic(harness)

    assert len(harness.ledger_path.read_text(encoding="utf-8").splitlines()) == 6


def test_a_clean_ledger_passes_every_check_and_is_certified(tmp_path):
    harness = build_harness(tmp_path)
    traffic(harness)

    assert_integrity_ok(harness.governance)
    assert_causal_chain_valid(harness.governance)
    assert_audit_passes(harness.governance, max_monitor_gap_seconds=GAP)

    certificate = harness.governance.certify(max_monitor_gap_seconds=GAP)

    assert certificate is not None
    assert certificate.entries_covered == 6


# =====================================================================
# LEDGER: tampering is detected and blocks certification
# =====================================================================

TAMPERS = {
    "payload": lambda p: tamper_payload(p, 0, amount=1),
    "hash": lambda p: tamper_entry_hash(p, 2),
    "link": lambda p: break_link(p, 3),
    "sequence_gap": lambda p: drop_entry(p, 2),
}


@pytest.mark.parametrize("kind", sorted(TAMPERS))
def test_tampering_is_detected_and_certification_is_refused(tmp_path, kind):
    harness = build_harness(tmp_path)
    traffic(harness)

    TAMPERS[kind](harness.ledger_path)

    assert_integrity_failed(harness.governance)
    assert harness.governance.certify(max_monitor_gap_seconds=GAP) is None


@pytest.mark.parametrize("kind", sorted(TAMPERS))
def test_the_cli_agrees_a_tampered_ledger_fails(tmp_path, kind):
    harness = build_harness(tmp_path)
    traffic(harness)
    TAMPERS[kind](harness.ledger_path)

    result = subprocess.run(
        [sys.executable, "-m", "vsl", "verify", "--ledger", str(harness.ledger_path)],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True, text=True,
    )

    assert result.returncode == 1, result.stdout


# =====================================================================
# LEDGER: truncation needs a checkpoint
# =====================================================================


def test_truncation_is_invisible_to_integrity_but_caught_by_a_checkpoint(tmp_path):
    harness = build_harness(tmp_path)
    traffic(harness)
    anchor = harness.governance.checkpoint()

    truncate(harness.ledger_path, keep=4)

    reopened = open_jsonl_ledger(harness.ledger_path)
    assert reopened.verify_integrity()                       # the known limitation

    inspection = inspect_ledger(reopened, checkpoint=anchor)
    assert inspection.integrity_ok and not inspection.ok
    assert inspection.checkpoint.code == "truncated"


def test_an_untouched_ledger_matches_its_checkpoint_even_after_growth(tmp_path):
    harness = build_harness(tmp_path)
    traffic(harness)
    anchor = harness.governance.checkpoint()

    traffic(harness)                                          # honest growth

    result = harness.governance.verify_checkpoint(anchor)

    assert isinstance(result, CheckpointVerification) and result.ok


# =====================================================================
# AUDIT and causal validation
# =====================================================================


def test_a_suspended_ledger_fails_the_audit_until_it_is_resolved(tmp_path):
    harness = build_harness(tmp_path)
    asyncio.run(suspend(harness.governance))

    report = harness.governance.audit(max_monitor_gap_seconds=GAP)
    assert not report.all_passed
    assert {
        "insufficient_verification_has_specification_update",
        "terminal_has_human_authorised_transition",
    } <= set(report.checks_failed)

    resolve_suspension(harness.governance)

    assert_audit_passes(harness.governance, max_monitor_gap_seconds=GAP)
    assert harness.governance.certify(max_monitor_gap_seconds=GAP) is not None


def test_a_broken_caused_by_chain_is_reported_with_the_exact_problem(tmp_path):
    from ledger_helpers import edit_line

    harness = build_harness(tmp_path)
    traffic(harness)
    edit_line(harness.ledger_path, 1, caused_by="no-such-entry")

    report = harness.governance.validate_caused_by()

    assert not report.valid and report.issues[0].code == "unknown_cause"


# =====================================================================
# POLICIES
# =====================================================================

BASE = EXPENSE_POLICY_YAML


def test_the_reference_policy_loads_and_governs(tmp_path):
    governance = load_policy_text(tmp_path, BASE)

    decision = asyncio.run(governance.authorize(EXPENSE_ACTION, ok_context()))

    assert decision.outcome is Outcome.PROCEED


REJECTED = {
    "malformed_yaml": ("agent: [unclosed", PolicyConfigError, "Malformed YAML"),
    "empty_file": ("", PolicyConfigError, "empty"),
    "duplicate_key": (BASE + "\nagent:\n  name: other\n", PolicyConfigError, "Duplicate key"),
    "unsupported_top_level_field": (BASE + "\nextra: 1\n", PolicySchemaError, "extra"),
    "wrong_type": (replace_in_policy(BASE, 'version: "1.0.0"', "version: 1"), PolicySchemaError, "string"),
    "secret_like_key": (
        replace_in_policy(BASE, "  name: expense-agent", "  name: expense-agent\n  api_key: abc"),
        PolicyConfigError, "secrets must never appear",
    ),
    "unknown_monitor": (
        replace_in_policy(BASE, "monitor: confidence_gamma", "monitor: nope"),
        RegistryError, "Unknown monitor",
    ),
    "code_in_rule_name": (
        replace_in_policy(BASE, "rule: max_value", "rule: __import__('os').system('x')"),
        PolicySchemaError, "does not match",
    ),
    "unknown_terminal_state": (
        replace_in_policy(BASE, "on_violation: expense-approvals-suspended", "on_violation: missing"),
        PolicyValidationError, "terminal state",
    ),
    "unknown_assurance_basis": (
        replace_in_policy(BASE, "assurance_basis: output_layer\n    gamma_threshold", "assurance_basis: nope\n    gamma_threshold"),
        PolicyValidationError, "assurance basis",
    ),
    "unknown_prenode_reference": (
        replace_in_policy(BASE, "prenodes: [approval-confidence]", "prenodes: [ghost]"),
        PolicyValidationError, "ghost",
    ),
    "assurance_level_cannot_be_set": (
        replace_in_policy(BASE, "f1_pre_commitment: true", "f1_pre_commitment: true\n    level: FULL"),
        PolicyValidationError, "cannot be set",
    ),
}


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_invalid_policies_are_rejected_with_a_specific_error(tmp_path, case):
    text, error_type, fragment = REJECTED[case]

    error = policy_error(tmp_path, text)

    assert type(error) is error_type
    assert fragment in " ".join(error_messages(error))


def test_policy_error_refuses_to_pass_an_accepted_policy(tmp_path):
    with pytest.raises(AssertionError, match="should be rejected"):
        policy_error(tmp_path, BASE)


def test_replace_in_policy_requires_exactly_one_match():
    with pytest.raises(AssertionError, match="occurs 0 times"):
        replace_in_policy(BASE, "no-such-text", "x")

    with pytest.raises(AssertionError, match="occurs"):
        replace_in_policy(BASE, "name:", "x")


def test_a_yaml_python_tag_is_rejected_and_nothing_executes(tmp_path):
    marker = tmp_path / "executed"
    text = BASE.replace(
        'version: "1.0.0"',
        f"version: !!python/object/apply:os.system ['touch {marker}']",
    )

    error = policy_error(tmp_path, text)

    assert isinstance(error, PolicyConfigError)
    assert not marker.exists()


def test_environment_interpolation_changes_behaviour_safely(tmp_path):
    text = replace_in_policy(BASE, "max: 2000", "max: ${EXPENSE_CAP:-2000}")

    default = load_policy_text(tmp_path, text)
    lowered = load_policy_text(tmp_path, text, env={"EXPENSE_CAP": "100"})

    assert asyncio.run(default.authorize(EXPENSE_ACTION, ok_context())).outcome is Outcome.PROCEED
    assert asyncio.run(lowered.authorize(EXPENSE_ACTION, ok_context())).outcome is Outcome.SUSPENDED


def test_an_interpolated_value_must_still_be_valid(tmp_path):
    text = replace_in_policy(BASE, "max: 2000", "max: ${CAP}")

    error = policy_error(tmp_path, text, env={"CAP": "abc"})

    assert isinstance(error, PolicyValidationError)


def test_a_missing_variable_without_a_default_is_an_error(tmp_path):
    text = replace_in_policy(BASE, "max: 2000", "max: ${CAP}")

    assert isinstance(policy_error(tmp_path, text), SDKError)


def test_every_rejection_is_an_sdk_error_never_a_bare_exception(tmp_path):
    for text, _, _ in REJECTED.values():
        assert isinstance(policy_error(tmp_path, text), SDKError)


# ---- equivalence with the programmatic policy ----------------------


@pytest.mark.parametrize(
    "context",
    [ok_context(), low_confidence_context(), over_cap_context(), ok_context(amount=2000), ok_context(confidence=0.9, amount=1999.99)],
)
def test_yaml_and_programmatic_policies_decide_identically(tmp_path, context):
    from_yaml = load_policy_text(tmp_path, BASE)
    programmatic = Governance(policy=build_policy(), ledger=memory_ledger())

    a = asyncio.run(from_yaml.authorize(EXPENSE_ACTION, dict(context)))
    b = asyncio.run(programmatic.authorize(EXPENSE_ACTION, dict(context)))

    assert (a.outcome, a.allowed, a.requires_human_review, a.suspended, a.terminal_state) == (
        b.outcome, b.allowed, b.requires_human_review, b.suspended, b.terminal_state
    )
