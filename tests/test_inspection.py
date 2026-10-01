"""
Phase 4 — inspect_ledger() and Governance.certify().

Certification must reflect actual evidence: integrity, the five audit
checks, caused_by validation, and (optionally) a checkpoint. It is never
granted merely because the call ran.
"""

from __future__ import annotations

import json

import pytest

from vsl import (
    Governance,
    inspect_ledger,
    jsonl_ledger,
    memory_ledger,
    open_jsonl_ledger,
)
from vsl.inspection import validate_gap

from ledger_helpers import (
    AGENT,
    clean_traffic,
    edit_line,
    edit_payload,
    entries,
    ledger_file,
    ok_ctx,
    read_lines,
    resolve,
    suspend,
    write_lines,
)
from vsl_core.ledger import (
    LedgerEntryType,
    VerbaCertificate,
    VerificationResult,
)

GAP = 300


# ================================================================
# GAP VALIDATION
# ================================================================


@pytest.mark.parametrize("value", [None, 0, 0.5, 300, 1e6])
def test_valid_gaps(value):
    assert validate_gap(value) == (None if value is None else float(value))


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "300", True, [1]])
def test_invalid_gaps(value):
    with pytest.raises(ValueError, match="max_monitor_gap_seconds"):
        validate_gap(value)


# ================================================================
# inspect_ledger: CLEAN
# ================================================================


def test_clean_ledger_inspects_ok(governance):
    clean_traffic(governance)

    result = inspect_ledger(governance.ledger, max_monitor_gap_seconds=GAP)

    assert result.ok and result.failures == ()
    assert result.integrity_ok
    assert result.audit.all_passed
    assert result.causal.valid
    assert result.entries_checked == 6
    assert result.checkpoint is None
    assert result.certificate is None          # not requested


def test_inspection_reuses_vsl_core_results(governance):
    clean_traffic(governance)

    result = inspect_ledger(governance.ledger, max_monitor_gap_seconds=GAP)

    assert result.audit == governance.audit(max_monitor_gap_seconds=GAP)
    assert result.integrity_ok is governance.verify_integrity()
    assert result.causal == governance.validate_caused_by()


def test_inspection_is_read_only(governance):
    clean_traffic(governance)
    before = ledger_file(governance).read_bytes()

    inspect_ledger(governance.ledger, max_monitor_gap_seconds=GAP, certify=True)

    assert ledger_file(governance).read_bytes() == before


# ================================================================
# TAMPER DETECTION
# ================================================================


def inspect_file(path, **kwargs):
    return inspect_ledger(open_jsonl_ledger(path), **kwargs)


def test_modified_payload_is_detected(governance):
    clean_traffic(governance)
    edit_payload(ledger_file(governance), 0, amount=1)

    result = inspect_file(ledger_file(governance), max_monitor_gap_seconds=GAP)

    assert not result.integrity_ok
    assert not result.ok
    assert any(f.startswith("integrity") for f in result.failures)
    # The process checks do not notice an edited payload.
    assert result.audit.all_passed


def test_broken_hash_is_detected(governance):
    clean_traffic(governance)
    edit_line(ledger_file(governance), 2, entry_hash="f" * 64)

    assert not inspect_file(ledger_file(governance)).integrity_ok


def test_broken_link_is_detected(governance):
    clean_traffic(governance)
    edit_line(ledger_file(governance), 3, prev_hash="0" * 64)

    assert not inspect_file(ledger_file(governance)).integrity_ok


def test_sequence_gap_is_detected(governance):
    clean_traffic(governance)
    lines = read_lines(ledger_file(governance))
    del lines[2]
    write_lines(ledger_file(governance), lines)

    assert not inspect_file(ledger_file(governance)).integrity_ok


def test_duplicate_sequence_is_detected(governance):
    clean_traffic(governance)
    lines = read_lines(ledger_file(governance))
    lines.insert(2, lines[1])
    write_lines(ledger_file(governance), lines)

    assert not inspect_file(ledger_file(governance)).integrity_ok


def test_reordered_entries_are_detected(governance):
    clean_traffic(governance)
    lines = read_lines(ledger_file(governance))
    lines[1], lines[2] = lines[2], lines[1]
    write_lines(ledger_file(governance), lines)

    assert not inspect_file(ledger_file(governance)).integrity_ok


def test_invalid_causal_relationship_is_detected(governance):
    """Rewrite one PRE_NODE's caused_by to point at nothing. Hashes are
    then wrong too, so integrity also fails; the causal report names
    the exact problem."""

    clean_traffic(governance)
    edit_line(ledger_file(governance), 1, caused_by="no-such-entry")

    result = inspect_file(ledger_file(governance))

    assert not result.causal.valid
    assert result.causal.issues[0].code == "unknown_cause"
    assert not result.integrity_ok


def test_causal_failure_with_valid_hashes_and_passing_audit(tmp_path):
    """The false-pass case: honest hashes, passing audit(), broken
    causal chain. Only caused_by validation catches it."""

    ledger = jsonl_ledger(tmp_path / "l.jsonl")

    ledger.write_monitor(identity_key=AGENT, instance_id="i", drift_detected=True)
    pre_node = ledger.write(
        LedgerEntryType.PRE_NODE,
        identity_key=AGENT,
        instance_id="i",
        payload={"denied": False},          # caused_by omitted
    )
    ledger.write_verification(
        identity_key=AGENT,
        instance_id="i",
        caused_by=pre_node.entry_id,
        result=VerificationResult.SUFFICIENT,
    )

    result = inspect_ledger(ledger, max_monitor_gap_seconds=GAP, certify=True)

    assert result.integrity_ok
    assert result.audit.all_passed
    assert not result.causal.valid
    assert result.certificate is None
    assert any(f.startswith("caused_by") for f in result.failures)


def test_invalid_ledger_structure_is_rejected_before_inspection(governance):
    from vsl import LedgerStorageError

    clean_traffic(governance)
    raw = json.loads(read_lines(ledger_file(governance))[0])
    del raw["entry_hash"]
    lines = read_lines(ledger_file(governance))
    lines[0] = json.dumps(raw)
    write_lines(ledger_file(governance), lines)

    with pytest.raises(LedgerStorageError):
        open_jsonl_ledger(ledger_file(governance))


# ================================================================
# CHECKPOINT INSIDE INSPECTION
# ================================================================


def test_inspection_with_matching_checkpoint_is_ok(governance):
    clean_traffic(governance)
    anchored = governance.checkpoint()

    result = inspect_ledger(governance.ledger, checkpoint=anchored)

    assert result.ok and result.checkpoint.ok


def test_inspection_with_truncated_ledger_fails_on_the_checkpoint(governance):
    clean_traffic(governance)
    anchored = governance.checkpoint()

    write_lines(ledger_file(governance), read_lines(ledger_file(governance))[:-2])

    result = inspect_file(ledger_file(governance), checkpoint=anchored)

    assert result.integrity_ok
    assert not result.ok
    assert any(f.startswith("checkpoint: truncated") for f in result.failures)


def test_inspection_without_checkpoint_does_not_claim_truncation_safety(governance):
    clean_traffic(governance)
    write_lines(ledger_file(governance), read_lines(ledger_file(governance))[:-2])

    result = inspect_file(ledger_file(governance))

    assert result.ok                       # the documented limitation
    assert result.checkpoint is None


# ================================================================
# CERTIFICATION
# ================================================================


def test_certify_issues_a_real_vsl_core_certificate_for_clean_evidence(governance):
    clean_traffic(governance)

    certificate = governance.certify(max_monitor_gap_seconds=GAP)

    assert isinstance(certificate, VerbaCertificate)
    assert certificate.entries_covered == 6
    assert certificate.audit_report.all_passed
    assert len(certificate.certificate_hash) == 64
    assert "governance process" in certificate.NOTE


def test_certify_after_the_full_lifecycle(governance):
    suspend(governance)
    resolve(governance)

    assert governance.certify(max_monitor_gap_seconds=GAP) is not None


def test_certify_with_matching_checkpoint(governance):
    clean_traffic(governance)

    assert governance.certify(
        max_monitor_gap_seconds=GAP, checkpoint=governance.checkpoint()
    ) is not None


def test_certify_refuses_while_suspended(governance):
    suspend(governance)

    assert governance.certify(max_monitor_gap_seconds=GAP) is None

    result = inspect_ledger(governance.ledger, max_monitor_gap_seconds=GAP, certify=True)

    assert any("insufficient_verification_has_specification_update" in f for f in result.failures)
    assert any("terminal_has_human_authorised_transition" in f for f in result.failures)


def test_certify_refuses_when_only_the_terminal_was_re_enabled(governance):
    suspend(governance)

    from vsl_core.governance import GovernanceAuthority
    from vsl_core.identity import Evidence

    governance.reenable(
        authority=GovernanceAuthority(name="A", escalation_contact="a@example.com"),
        authorised_by="j.doe",
        evidence=Evidence(root_cause_analysis="x", specification_update_proposal="y"),
    )

    assert governance.certify(max_monitor_gap_seconds=GAP) is None


def test_certify_refuses_tampered_ledger_even_though_audit_passes(governance):
    clean_traffic(governance)
    edit_payload(ledger_file(governance), 0, amount=1)

    tampered = Governance(
        policy=governance.policy,
        ledger=open_jsonl_ledger(ledger_file(governance)),
    )

    assert tampered.audit().all_passed
    assert tampered.verify_integrity() is False
    assert tampered.certify(max_monitor_gap_seconds=GAP) is None


def test_certify_refuses_a_checkpoint_mismatch(governance, tmp_path):
    clean_traffic(governance)

    other = jsonl_ledger(tmp_path / "other.jsonl")
    for _ in range(6):
        other.write_monitor(identity_key=AGENT, instance_id="i", drift_detected=False)

    assert governance.certify(
        max_monitor_gap_seconds=GAP, checkpoint=other.current_checkpoint()
    ) is None


def test_certify_refuses_a_truncated_ledger_when_a_checkpoint_is_given(governance):
    clean_traffic(governance)
    anchored = governance.checkpoint()

    write_lines(ledger_file(governance), read_lines(ledger_file(governance))[:-1])

    truncated = Governance(
        policy=governance.policy,
        ledger=open_jsonl_ledger(ledger_file(governance)),
    )

    assert truncated.certify(max_monitor_gap_seconds=GAP, checkpoint=anchored) is None


def test_certify_refuses_an_empty_ledger():
    from governance.policy import build_policy

    empty = Governance(policy=build_policy(), ledger=memory_ledger())

    assert empty.certify(max_monitor_gap_seconds=GAP) is None

    result = inspect_ledger(empty.ledger, max_monitor_gap_seconds=GAP, certify=True)

    assert any("empty" in f for f in result.failures)


def test_certify_requires_a_monitoring_gap_threshold(governance):
    clean_traffic(governance)

    with pytest.raises(ValueError, match="required"):
        governance.certify(max_monitor_gap_seconds=None)


@pytest.mark.parametrize("bad", [-1, float("nan"), "300"])
def test_certify_rejects_an_invalid_threshold(governance, bad):
    with pytest.raises(ValueError, match="max_monitor_gap_seconds"):
        governance.certify(max_monitor_gap_seconds=bad)


def test_certify_detects_a_monitoring_gap(governance):
    import time

    import asyncio

    asyncio.run(governance.authorize("approve_expense", ok_ctx()))
    time.sleep(0.05)
    asyncio.run(governance.authorize("approve_expense", ok_ctx()))

    assert governance.certify(max_monitor_gap_seconds=0.01) is None
    assert governance.certify(max_monitor_gap_seconds=GAP) is not None


def test_a_certificate_is_only_issued_when_there_are_no_failures(governance):
    clean_traffic(governance)

    ok = inspect_ledger(governance.ledger, max_monitor_gap_seconds=GAP, certify=True)

    assert ok.certificate is not None and ok.failures == ()

    suspend(governance)

    bad = inspect_ledger(governance.ledger, max_monitor_gap_seconds=GAP, certify=True)

    assert bad.certificate is None and bad.failures


def test_certify_does_not_alter_the_ledger(governance):
    clean_traffic(governance)
    before = ledger_file(governance).read_bytes()

    governance.certify(max_monitor_gap_seconds=GAP)

    assert ledger_file(governance).read_bytes() == before
