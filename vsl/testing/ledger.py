"""Ledger inspection and tampering helpers for governance tests.

Inspection helpers read any ledger. The ``tamper_*`` / ``drop_entry`` /
``truncate`` helpers edit a JSONL ledger *file* behind VSL-Core's back, so
tests can prove that VerbaLedger verification, the audit, checkpoints and
the CLI notice. They deliberately never recompute hashes: a tampered
ledger must look tampered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vsl_core.ledger import LedgerEntryType


def _ledger_of(source: Any) -> Any:
    """Accept a harness, a Governance, or a VerbaLedger."""

    governance = getattr(source, "governance", None)

    if governance is not None:
        source = governance

    ledger = getattr(source, "ledger", None)

    return ledger if ledger is not None else source


def ledger_entries(source: Any) -> list[Any]:
    """Every entry in the ledger, in sequence order."""

    return list(_ledger_of(source).store.all_entries())


def entries_for_decision(source: Any, decision_id: str) -> list[Any]:
    """Entries written for one governance decision, in order."""

    return [
        entry
        for entry in ledger_entries(source)
        if entry.decision_id == decision_id
    ]


def latest_decision_id(source: Any) -> str:
    """The decision id of the most recently written entry."""

    entries = ledger_entries(source)

    if not entries:
        raise AssertionError("the ledger is empty")

    decision_id = entries[-1].decision_id

    if decision_id is None:
        raise AssertionError("the latest ledger entry has no decision_id")

    return decision_id


def entry_types(entries: list[Any]) -> list[LedgerEntryType]:
    return [entry.entry_type for entry in entries]


# ---------------------------------------------------------------------
# TAMPERING (JSONL files only; hashes are never recomputed)
# ---------------------------------------------------------------------


def _read(path: str | Path) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines()


def _write(path: str | Path, lines: list[str]) -> None:
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _edit_line(path: str | Path, index: int, edit: Any) -> None:
    lines = _read(path)
    raw = json.loads(lines[index])
    edit(raw)
    lines[index] = json.dumps(raw, sort_keys=True)
    _write(path, lines)


def tamper_payload(path: str | Path, index: int, **changes: Any) -> None:
    """Change fields inside entry ``index``'s payload."""

    _edit_line(path, index, lambda raw: raw["payload"].update(changes))


def tamper_entry_hash(path: str | Path, index: int) -> None:
    """Overwrite entry ``index``'s stored hash."""

    _edit_line(path, index, lambda raw: raw.update(entry_hash="f" * 64))


def break_link(path: str | Path, index: int) -> None:
    """Make entry ``index`` point at a previous hash that is not real."""

    _edit_line(path, index, lambda raw: raw.update(prev_hash="0" * 64))


def drop_entry(path: str | Path, index: int) -> None:
    """Delete one line from the middle of the file (a sequence gap)."""

    lines = _read(path)
    del lines[index]
    _write(path, lines)


def truncate(path: str | Path, keep: int) -> None:
    """Keep only the first ``keep`` entries.

    A shorter chain is still a valid chain, so ``verify_integrity()``
    cannot see this. Only a checkpoint anchored beforehand can.
    """

    _write(path, _read(path)[:keep])
