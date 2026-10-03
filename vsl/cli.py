"""Command line: ``vsl verify | audit | certify | scan``.

    vsl verify  --ledger FILE [--checkpoint FILE] [--write-checkpoint FILE]
    vsl audit   --ledger FILE [--max-monitor-gap SECONDS]
    vsl certify --ledger FILE --max-monitor-gap SECONDS [--checkpoint FILE]
    vsl scan    [ARGS...]     runs ``x-verba scan ARGS...`` (optional extra)

Exit status
-----------
0  everything requested passed
1  a check failed: integrity, audit, causal chain, checkpoint
   mismatch, or certification requirements not met
2  invalid usage or input: bad arguments, unreadable or invalid
   checkpoint file, refusing to overwrite a checkpoint
3  the ledger could not be read: missing, malformed or structurally
   invalid

``vsl scan`` returns the exit status of ``x-verba scan`` unchanged, or 127
if the ``x-verba`` executable is not installed.

The CLI only reads the ledger. It never creates, repairs or rewrites
ledger files. It is a thin wrapper over the SDK and VSL-Core calls
described in vsl.inspection.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from vsl_core.ledger import LedgerCheckpoint

from .checkpoint import (
    export_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from .exceptions import (
    CheckpointError,
    CLIUsageError,
    LedgerStorageError,
    SDKError,
)
from .inspection import LedgerInspection, inspect_ledger
from .ledger import open_jsonl_ledger

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_LEDGER = 3
EXIT_SCAN_UNAVAILABLE = 127

FIVE_CHECKS = (
    "no_monitoring_gaps",
    "drift_flagged_monitor_has_pre_node",
    "pre_node_has_verification",
    "insufficient_verification_has_specification_update",
    "terminal_has_human_authorised_transition",
)


# =====================================================================
# ARGUMENTS
# =====================================================================


def _gap(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a number of seconds"
        ) from None

    if value != value or value in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError(
            "must be a finite number of seconds"
        )

    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")

    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vsl",
        description=(
            "Verify, audit and certify a VerbaLedger. "
            "Exit status: 0 ok, 1 check failed, 2 usage/input "
            "error, 3 ledger unreadable."
        ),
    )

    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser(
        "verify",
        help="were the records edited? (hash chain integrity)",
    )
    verify.add_argument("--ledger", required=True, metavar="FILE")
    verify.add_argument(
        "--checkpoint",
        metavar="FILE",
        help="also compare against an anchored checkpoint "
        "(detects truncation)",
    )
    verify.add_argument(
        "--write-checkpoint",
        metavar="FILE",
        help="after a clean verification, write the ledger tip as a "
        "new checkpoint file (never overwrites)",
    )

    audit = commands.add_parser(
        "audit",
        help="do the records describe a governed process? "
        "(five audit checks plus caused_by validation)",
    )
    audit.add_argument("--ledger", required=True, metavar="FILE")
    audit.add_argument(
        "--max-monitor-gap",
        type=_gap,
        metavar="SECONDS",
        help="audit check 1 threshold; without it check 1 is skipped",
    )

    certify = commands.add_parser(
        "certify",
        help="issue a VSL-Core certificate only if integrity, "
        "audit and causal validation all pass",
    )
    certify.add_argument("--ledger", required=True, metavar="FILE")
    certify.add_argument(
        "--max-monitor-gap",
        type=_gap,
        required=True,
        metavar="SECONDS",
        help="audit check 1 threshold (required to certify)",
    )
    certify.add_argument(
        "--checkpoint",
        metavar="FILE",
        help="also require a match against an anchored checkpoint",
    )

    commands.add_parser(
        "scan",
        help="run X-Verba Scan ('x-verba scan ARGS...'); all arguments "
        "after 'scan' are passed to it unchanged",
    )

    return parser


# =====================================================================
# REPORTING
# =====================================================================


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _print_checkpoint(inspection: LedgerInspection) -> None:
    result = inspection.checkpoint

    if result is None:
        return

    if result.ok:
        _out(
            f"Checkpoint: OK ({result.detail})"
        )
    else:
        _out(f"Checkpoint: FAILED [{result.code}] {result.detail}")


def _print_audit(
    inspection: LedgerInspection,
    *,
    gap_given: bool,
) -> None:
    report = inspection.audit

    for check in FIVE_CHECKS:
        if check == FIVE_CHECKS[0] and not gap_given:
            _out(
                f"  SKIPPED  {check} "
                "(no --max-monitor-gap given)"
            )
            continue

        if check in report.checks_failed:
            ids = ", ".join(report.violations[check])
            _out(f"  FAILED   {check}: {ids}")
        else:
            _out(f"  passed   {check}")

    causal = inspection.causal

    if causal.valid:
        _out(
            "  passed   caused_by chain "
            f"({causal.entries_checked} entries)"
        )
    else:
        _out(
            f"  FAILED   caused_by chain: {len(causal.issues)} "
            "issue(s)"
        )

        for issue in causal.issues:
            _out(
                f"             {issue.entry_id} "
                f"({issue.entry_type.value}) "
                f"[{issue.code}] {issue.detail}"
            )


# =====================================================================
# COMMANDS
# =====================================================================


def _load_checkpoint(path: str | None) -> LedgerCheckpoint | None:
    return load_checkpoint(path) if path else None


def _cmd_verify(args: argparse.Namespace) -> int:
    ledger = open_jsonl_ledger(args.ledger)
    anchor = _load_checkpoint(args.checkpoint)

    inspection = inspect_ledger(ledger, checkpoint=anchor)

    _out(f"Ledger: {args.ledger} ({inspection.entries_checked} entries)")

    verified = inspection.integrity_ok

    _out(
        "Integrity: "
        + (
            "OK (hash chain, links and sequence recomputed by VSL-Core)"
            if verified
            else "FAILED (an entry was edited, a link is broken, or "
            "the sequence has a gap)"
        )
    )

    _print_checkpoint(inspection)

    if anchor is None:
        _out(
            "Note: integrity verification cannot detect truncation. "
            "Anchor a checkpoint and re-run with --checkpoint FILE."
        )

    if inspection.checkpoint is not None and not inspection.checkpoint.ok:
        verified = False

    if not verified:
        _out("RESULT: FAILED")
        return EXIT_FAILED

    if args.write_checkpoint:
        written = save_checkpoint(
            export_checkpoint(ledger),
            args.write_checkpoint,
        )
        _out(f"Checkpoint written: {written}")

    _out("RESULT: OK")
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace) -> int:
    ledger = open_jsonl_ledger(args.ledger)

    inspection = inspect_ledger(
        ledger,
        max_monitor_gap_seconds=args.max_monitor_gap,
    )

    _out(f"Ledger: {args.ledger} ({inspection.entries_checked} entries)")
    _out("Audit:")

    _print_audit(
        inspection,
        gap_given=args.max_monitor_gap is not None,
    )

    _out(
        "Note: audit does not detect tampering. "
        "Run `vsl verify` as well."
    )

    passed = inspection.audit.all_passed and inspection.causal.valid

    _out("RESULT: " + ("OK" if passed else "FAILED"))

    return EXIT_OK if passed else EXIT_FAILED


def _cmd_certify(args: argparse.Namespace) -> int:
    ledger = open_jsonl_ledger(args.ledger)
    anchor = _load_checkpoint(args.checkpoint)

    inspection = inspect_ledger(
        ledger,
        max_monitor_gap_seconds=args.max_monitor_gap,
        checkpoint=anchor,
        certify=True,
    )

    _out(f"Ledger: {args.ledger} ({inspection.entries_checked} entries)")

    if inspection.certificate is None:
        _out("Certificate: NOT ISSUED")

        for failure in inspection.failures:
            _out(f"  - {failure}")

        _out("RESULT: FAILED")
        return EXIT_FAILED

    certificate = inspection.certificate

    _out("Certificate: ISSUED")
    _out(f"  entries covered:  {certificate.entries_covered}")
    _out(f"  period start:     {certificate.period_start}")
    _out(f"  period end:       {certificate.period_end}")
    _out(f"  certificate hash: {certificate.certificate_hash}")
    _out(f"  {certificate.NOTE}")

    _out("RESULT: OK")
    return EXIT_OK


_COMMANDS = {
    "verify": _cmd_verify,
    "audit": _cmd_audit,
    "certify": _cmd_certify,
}


# =====================================================================
# ENTRY POINT
# =====================================================================


def _scan(args: Sequence[str]) -> int:
    from .integrations.scan import ScannerNotFoundError, run_scan

    try:
        return run_scan(args)

    except ScannerNotFoundError as exc:
        _err(f"error: {exc}")
        return EXIT_SCAN_UNAVAILABLE


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # ``scan`` forwards everything after it to the scanner, so it is
    # handled before argparse can interpret those arguments.
    if arguments and arguments[0] == "scan":
        return _scan(arguments[1:])

    parser = build_parser()

    try:
        args = parser.parse_args(arguments)

    except SystemExit as exc:
        code = exc.code

        return code if isinstance(code, int) else EXIT_USAGE

    try:
        return _COMMANDS[args.command](args)

    except LedgerStorageError as exc:
        _err(f"error: {exc}")
        return EXIT_LEDGER

    except (CheckpointError, CLIUsageError) as exc:
        _err(f"error: {exc}")
        return EXIT_USAGE

    except SDKError as exc:
        _err(f"error: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
