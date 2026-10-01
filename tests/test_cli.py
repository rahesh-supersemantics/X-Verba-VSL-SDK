"""
Phase 4 — the ``vsl verify | audit | certify`` command line.

Exit codes: 0 ok, 1 check failed, 2 usage/input error, 3 ledger unreadable.
The CLI only reads ledgers; several tests prove the bytes never change.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from vsl import jsonl_ledger
from vsl.cli import EXIT_FAILED, EXIT_LEDGER, EXIT_OK, EXIT_USAGE, main
from vsl_core.ledger import LedgerEntryType, VerificationResult

from ledger_helpers import (
    AGENT,
    clean_traffic,
    edit_line,
    edit_payload,
    ledger_file,
    read_lines,
    resolve,
    suspend,
    write_lines,
)

ROOT = Path(__file__).resolve().parent.parent
GAP = "300"


def run(capsys, *argv):
    code = main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def clean(governance):
    clean_traffic(governance)
    return ledger_file(governance)


# ================================================================
# verify
# ================================================================


def test_verify_clean_ledger(capsys, clean):
    code, out, _ = run(capsys, "verify", "--ledger", clean)

    assert code == EXIT_OK
    assert "Integrity: OK" in out
    assert "RESULT: OK" in out


def test_verify_notes_truncation_limitation_without_checkpoint(capsys, clean):
    _, out, _ = run(capsys, "verify", "--ledger", clean)

    assert "cannot detect truncation" in out


def test_verify_detects_modified_payload(capsys, clean):
    edit_payload(clean, 0, amount=1)

    code, out, _ = run(capsys, "verify", "--ledger", clean)

    assert code == EXIT_FAILED
    assert "Integrity: FAILED" in out
    assert "RESULT: FAILED" in out


def test_verify_detects_broken_hash(capsys, clean):
    edit_line(clean, 2, entry_hash="0" * 64)

    code, _, _ = run(capsys, "verify", "--ledger", clean)

    assert code == EXIT_FAILED


def test_verify_detects_broken_link(capsys, clean):
    edit_line(clean, 2, prev_hash="f" * 64)

    code, _, _ = run(capsys, "verify", "--ledger", clean)

    assert code == EXIT_FAILED


def test_verify_detects_sequence_gap(capsys, clean):
    lines = read_lines(clean)
    del lines[2]
    write_lines(clean, lines)

    code, _, _ = run(capsys, "verify", "--ledger", clean)

    assert code == EXIT_FAILED


def test_verify_truncation_without_checkpoint_is_the_documented_limitation(
    capsys, clean
):
    write_lines(clean, read_lines(clean)[:-2])

    code, out, _ = run(capsys, "verify", "--ledger", clean)

    assert code == EXIT_OK
    assert "cannot detect truncation" in out


def test_verify_truncation_with_checkpoint_fails(capsys, governance, clean, tmp_path):
    cp = tmp_path / "cp.json"
    assert run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)[0] == 0

    write_lines(clean, read_lines(clean)[:-2])

    code, out, _ = run(capsys, "verify", "--ledger", clean, "--checkpoint", cp)

    assert code == EXIT_FAILED
    assert "Checkpoint: FAILED [truncated]" in out
    assert "cannot detect truncation" not in out


def test_verify_matching_checkpoint_passes(capsys, clean, tmp_path):
    cp = tmp_path / "cp.json"
    run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)

    code, out, _ = run(capsys, "verify", "--ledger", clean, "--checkpoint", cp)

    assert code == EXIT_OK
    assert "Checkpoint: OK" in out


def test_verify_checkpoint_still_matches_after_honest_growth(
    capsys, governance, clean, tmp_path
):
    cp = tmp_path / "cp.json"
    run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)

    clean_traffic(governance)

    code, _, _ = run(capsys, "verify", "--ledger", clean, "--checkpoint", cp)

    assert code == EXIT_OK


def test_verify_checkpoint_mismatch_fails(capsys, clean, tmp_path):
    cp = tmp_path / "cp.json"
    run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)

    other = tmp_path / "other.jsonl"
    ledger = jsonl_ledger(other)
    for _ in range(8):
        ledger.write_monitor(
            identity_key=AGENT, instance_id="x", drift_detected=False
        )

    code, out, _ = run(capsys, "verify", "--ledger", other, "--checkpoint", cp)

    assert code == EXIT_FAILED
    assert "Checkpoint: FAILED" in out


def test_verify_missing_checkpoint_file_is_usage_error(capsys, clean, tmp_path):
    code, _, err = run(
        capsys, "verify", "--ledger", clean, "--checkpoint", tmp_path / "no.json"
    )

    assert code == EXIT_USAGE
    assert err.startswith("error:")


def test_verify_invalid_checkpoint_file_is_usage_error(capsys, clean, tmp_path):
    cp = tmp_path / "bad.json"
    cp.write_text("{not json", encoding="utf-8")

    code, _, _ = run(capsys, "verify", "--ledger", clean, "--checkpoint", cp)

    assert code == EXIT_USAGE


def test_verify_checkpoint_with_extra_fields_is_rejected(capsys, clean, tmp_path):
    cp = tmp_path / "cp.json"
    run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)
    cp.write_text(
        cp.read_text(encoding="utf-8").rstrip().rstrip("}") + ', "extra": 1}',
        encoding="utf-8",
    )

    code, _, _ = run(capsys, "verify", "--ledger", clean, "--checkpoint", cp)

    assert code == EXIT_USAGE


def test_write_checkpoint_after_clean_verify(capsys, clean, tmp_path):
    cp = tmp_path / "cp.json"

    code, out, _ = run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)

    assert code == EXIT_OK
    assert cp.exists()
    assert "Checkpoint written" in out


def test_write_checkpoint_refuses_to_overwrite(capsys, clean, tmp_path):
    cp = tmp_path / "cp.json"
    cp.write_text("precious", encoding="utf-8")

    code, _, err = run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)

    assert code == EXIT_USAGE
    assert cp.read_text(encoding="utf-8") == "precious"
    assert err.startswith("error:")


def test_checkpoint_is_not_written_after_integrity_failure(capsys, clean, tmp_path):
    edit_payload(clean, 0, amount=1)
    cp = tmp_path / "cp.json"

    code, _, _ = run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)

    assert code == EXIT_FAILED
    assert not cp.exists()


def test_checkpoint_is_not_written_when_truncation_is_caught(
    capsys, clean, tmp_path
):
    first = tmp_path / "first.json"
    run(capsys, "verify", "--ledger", clean, "--write-checkpoint", first)
    write_lines(clean, read_lines(clean)[:-2])
    second = tmp_path / "second.json"

    code, _, _ = run(
        capsys,
        "verify",
        "--ledger",
        clean,
        "--checkpoint",
        first,
        "--write-checkpoint",
        second,
    )

    assert code == EXIT_FAILED
    assert not second.exists()


def test_write_checkpoint_on_empty_ledger_fails(capsys, tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    cp = tmp_path / "cp.json"

    code, _, _ = run(capsys, "verify", "--ledger", empty, "--write-checkpoint", cp)

    assert code == EXIT_USAGE
    assert not cp.exists()


# ---- unreadable ledgers -------------------------------------------


@pytest.mark.parametrize("command", ["verify", "audit"])
def test_missing_ledger_is_exit_3_and_is_not_created(capsys, tmp_path, command):
    missing = tmp_path / "nope.jsonl"

    code, _, err = run(capsys, command, "--ledger", missing)

    assert code == EXIT_LEDGER
    assert err.startswith("error:")
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_missing_ledger_for_certify_is_exit_3(capsys, tmp_path):
    code, _, _ = run(
        capsys, "certify", "--ledger", tmp_path / "x.jsonl", "--max-monitor-gap", GAP
    )

    assert code == EXIT_LEDGER


@pytest.mark.parametrize("command", ["verify", "audit"])
def test_malformed_jsonl_is_exit_3(capsys, clean, command):
    with clean.open("a", encoding="utf-8") as f:
        f.write("{this is not json\n")

    code, _, _ = run(capsys, command, "--ledger", clean)

    assert code == EXIT_LEDGER


@pytest.mark.parametrize("command", ["verify", "audit"])
def test_directory_as_ledger_is_exit_3(capsys, tmp_path, command):
    code, _, _ = run(capsys, command, "--ledger", tmp_path)

    assert code == EXIT_LEDGER


@pytest.mark.parametrize("command", ["verify", "audit"])
def test_structurally_invalid_entry_is_exit_3(capsys, clean, command):
    edit_line(clean, 1, entry_type="NOT_A_TYPE")

    code, _, _ = run(capsys, command, "--ledger", clean)

    assert code == EXIT_LEDGER


# ================================================================
# audit
# ================================================================


def test_audit_clean_ledger(capsys, clean):
    code, out, _ = run(capsys, "audit", "--ledger", clean, "--max-monitor-gap", GAP)

    assert code == EXIT_OK
    assert "passed   no_monitoring_gaps" in out
    assert "passed   caused_by chain" in out
    assert "RESULT: OK" in out


def test_audit_without_gap_reports_check_1_skipped(capsys, clean):
    code, out, _ = run(capsys, "audit", "--ledger", clean)

    assert code == EXIT_OK
    assert "SKIPPED  no_monitoring_gaps" in out
    assert "passed   no_monitoring_gaps" not in out


def test_audit_states_it_does_not_detect_tampering(capsys, clean):
    _, out, _ = run(capsys, "audit", "--ledger", clean)

    assert "does not detect tampering" in out


def test_audit_lists_all_five_checks(capsys, clean):
    _, out, _ = run(capsys, "audit", "--ledger", clean, "--max-monitor-gap", GAP)

    for name in (
        "no_monitoring_gaps",
        "drift_flagged_monitor_has_pre_node",
        "pre_node_has_verification",
        "insufficient_verification_has_specification_update",
        "terminal_has_human_authorised_transition",
    ):
        assert name in out


def test_audit_of_suspended_ledger_fails_checks_4_and_5(capsys, governance):
    suspend(governance)

    code, out, _ = run(
        capsys, "audit", "--ledger", ledger_file(governance), "--max-monitor-gap", GAP
    )

    assert code == EXIT_FAILED
    assert "FAILED   insufficient_verification_has_specification_update" in out
    assert "FAILED   terminal_has_human_authorised_transition" in out


def test_audit_passes_after_full_lifecycle(capsys, governance):
    suspend(governance)
    resolve(governance)

    code, out, _ = run(
        capsys, "audit", "--ledger", ledger_file(governance), "--max-monitor-gap", GAP
    )

    assert code == EXIT_OK, out


def test_audit_gap_threshold_fails(capsys, clean):
    code, out, _ = run(capsys, "audit", "--ledger", clean, "--max-monitor-gap", "0")

    assert code == EXIT_FAILED
    assert "FAILED   no_monitoring_gaps" in out


def test_audit_false_pass_is_caught_by_causal_validation(capsys, tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = jsonl_ledger(path)
    ledger.write_monitor(identity_key=AGENT, instance_id="i", drift_detected=True)
    pre_node = ledger.write(
        LedgerEntryType.PRE_NODE,
        identity_key=AGENT,
        instance_id="i",
        payload={"denied": False},
    )
    ledger.write_verification(
        identity_key=AGENT,
        instance_id="i",
        caused_by=pre_node.entry_id,
        result=VerificationResult.SUFFICIENT,
    )

    code, out, _ = run(capsys, "audit", "--ledger", path, "--max-monitor-gap", GAP)

    assert code == EXIT_FAILED
    assert "FAILED   caused_by chain" in out


@pytest.mark.parametrize("bad", ["-1", "abc", "nan", "inf"])
def test_audit_invalid_gap_is_usage_error(capsys, clean, bad):
    code, _, err = run(capsys, "audit", "--ledger", clean, "--max-monitor-gap", bad)

    assert code == EXIT_USAGE
    assert "max-monitor-gap" in err


# ================================================================
# certify
# ================================================================


def test_certify_clean_ledger_issues_certificate(capsys, clean):
    code, out, _ = run(capsys, "certify", "--ledger", clean, "--max-monitor-gap", GAP)

    assert code == EXIT_OK
    assert "Certificate: ISSUED" in out
    assert "certificate hash:" in out
    assert "RESULT: OK" in out


def test_certify_requires_a_gap(capsys, clean):
    code, _, err = run(capsys, "certify", "--ledger", clean)

    assert code == EXIT_USAGE
    assert "max-monitor-gap" in err


def test_certify_refuses_suspended_ledger(capsys, governance):
    suspend(governance)

    code, out, _ = run(
        capsys, "certify", "--ledger", ledger_file(governance), "--max-monitor-gap", GAP
    )

    assert code == EXIT_FAILED
    assert "Certificate: NOT ISSUED" in out


def test_certify_after_full_lifecycle(capsys, governance):
    suspend(governance)
    resolve(governance)

    code, out, _ = run(
        capsys, "certify", "--ledger", ledger_file(governance), "--max-monitor-gap", GAP
    )

    assert code == EXIT_OK, out


def test_certify_refuses_tampered_ledger(capsys, clean):
    edit_payload(clean, 0, amount=1)

    code, out, _ = run(capsys, "certify", "--ledger", clean, "--max-monitor-gap", GAP)

    assert code == EXIT_FAILED
    assert "Certificate: NOT ISSUED" in out


def test_certify_refuses_causal_failure(capsys, tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = jsonl_ledger(path)
    ledger.write_monitor(identity_key=AGENT, instance_id="i", drift_detected=True)
    pre_node = ledger.write(
        LedgerEntryType.PRE_NODE,
        identity_key=AGENT,
        instance_id="i",
        payload={"denied": False},
    )
    ledger.write_verification(
        identity_key=AGENT,
        instance_id="i",
        caused_by=pre_node.entry_id,
        result=VerificationResult.SUFFICIENT,
    )

    code, out, _ = run(capsys, "certify", "--ledger", path, "--max-monitor-gap", GAP)

    assert code == EXIT_FAILED
    assert "NOT ISSUED" in out


def test_certify_refuses_empty_ledger(capsys, tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    code, out, _ = run(capsys, "certify", "--ledger", empty, "--max-monitor-gap", GAP)

    assert code == EXIT_FAILED
    assert "NOT ISSUED" in out


def test_certify_with_matching_checkpoint(capsys, clean, tmp_path):
    cp = tmp_path / "cp.json"
    run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)

    code, out, _ = run(
        capsys,
        "certify",
        "--ledger",
        clean,
        "--max-monitor-gap",
        GAP,
        "--checkpoint",
        cp,
    )

    assert code == EXIT_OK
    assert "Certificate: ISSUED" in out


def test_certify_refuses_truncated_ledger_with_checkpoint(capsys, clean, tmp_path):
    cp = tmp_path / "cp.json"
    run(capsys, "verify", "--ledger", clean, "--write-checkpoint", cp)
    write_lines(clean, read_lines(clean)[:-2])

    code, out, _ = run(
        capsys,
        "certify",
        "--ledger",
        clean,
        "--max-monitor-gap",
        GAP,
        "--checkpoint",
        cp,
    )

    assert code == EXIT_FAILED
    assert "NOT ISSUED" in out
    assert "truncated" in out


def test_certify_with_bad_checkpoint_file_is_usage_error(capsys, clean, tmp_path):
    code, _, _ = run(
        capsys,
        "certify",
        "--ledger",
        clean,
        "--max-monitor-gap",
        GAP,
        "--checkpoint",
        tmp_path / "missing.json",
    )

    assert code == EXIT_USAGE


# ================================================================
# usage
# ================================================================


def test_no_arguments_is_usage_error(capsys):
    assert run(capsys)[0] == EXIT_USAGE


def test_unknown_command_is_usage_error(capsys):
    assert run(capsys, "frobnicate")[0] == EXIT_USAGE


@pytest.mark.parametrize("command", ["verify", "audit"])
def test_missing_ledger_argument_is_usage_error(capsys, command):
    assert run(capsys, command)[0] == EXIT_USAGE


def test_unknown_flag_is_usage_error(capsys, clean):
    assert run(capsys, "verify", "--ledger", clean, "--fix")[0] == EXIT_USAGE


def test_help_exits_zero(capsys):
    code, out, _ = run(capsys, "--help")

    assert code == EXIT_OK
    assert "verify" in out and "audit" in out and "certify" in out


# ================================================================
# read-only guarantee
# ================================================================


@pytest.mark.parametrize(
    "argv",
    [
        ("verify",),
        ("audit", "--max-monitor-gap", GAP),
        ("certify", "--max-monitor-gap", GAP),
    ],
)
def test_cli_never_modifies_a_clean_ledger(capsys, clean, argv):
    before = digest(clean)
    listing = sorted(p.name for p in clean.parent.iterdir())

    run(capsys, argv[0], "--ledger", clean, *argv[1:])

    assert digest(clean) == before
    assert sorted(p.name for p in clean.parent.iterdir()) == listing


@pytest.mark.parametrize("command", ["verify", "audit"])
def test_cli_never_repairs_a_tampered_ledger(capsys, clean, command):
    edit_payload(clean, 0, amount=1)
    before = digest(clean)

    run(capsys, command, "--ledger", clean)

    assert digest(clean) == before


# ================================================================
# real process
# ================================================================


def test_python_dash_m_vsl_runs_as_a_process(clean):
    ok = subprocess.run(
        [sys.executable, "-m", "vsl", "verify", "--ledger", str(clean)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0
    assert "RESULT: OK" in ok.stdout

    edit_payload(clean, 0, amount=1)

    bad = subprocess.run(
        [sys.executable, "-m", "vsl", "verify", "--ledger", str(clean)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1

    missing = subprocess.run(
        [sys.executable, "-m", "vsl", "verify", "--ledger", str(clean) + ".nope"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 3
