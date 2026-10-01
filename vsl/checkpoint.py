"""Checkpoint export and verification.

VerbaLedger.verify_integrity() proves internal consistency of whatever
entries the store currently holds. It CANNOT detect truncation, or
wholesale replacement with an older or forged chain that is
internally consistent. VSL-Core's answer (see its LedgerCheckpoint
docstring) is an external anchor: record the chain tip as
(sequence, entry_hash) somewhere the operator cannot rewrite, and compare
later. VSL-Core stops at exposing the tip; it ships no storage, signing
or comparison. This module adds only the file format and the comparison.

Checkpoint format
-----------------
Exactly the three fields of vsl_core.ledger.LedgerCheckpoint, as JSON:

    {"sequence": 41, "entry_hash": "<64 hex>", "checked_at": 1790000000.0}

Verification rule
-----------------
VSL-Core: "a later checkpoint with a lower sequence, or the same
sequence with a different hash, proves the local chain was altered."
Extended in the obvious way for a ledger that has legitimately grown
since the anchor: the ledger must still hold, at the anchored
sequence, an entry with the anchored hash.

    no entries, or tip below anchor   -> truncated
    no entry at the anchored sequence -> missing_entry
    different hash at that sequence   -> mismatch
    otherwise                         -> ok

A checkpoint is only as trustworthy as where it is stored. This module
writes a file; keeping it out of the operator's reach is the caller's
job. Run verify_integrity() as well: a checkpoint match does not prove
the entries before the anchor are unedited.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vsl_core.ledger import LedgerCheckpoint, VerbaLedger

from .exceptions import CheckpointError

_FIELDS = frozenset({"sequence", "entry_hash", "checked_at"})
_HASH = re.compile(r"^[0-9a-f]{64}$")

OK = "ok"
TRUNCATED = "truncated"
MISSING_ENTRY = "missing_entry"
MISMATCH = "mismatch"


@dataclass(frozen=True)
class CheckpointVerification:
    """Outcome of comparing a ledger against an anchored checkpoint."""

    ok: bool
    code: str
    detail: str
    anchored_sequence: int
    ledger_tip_sequence: int | None


# =====================================================================
# EXPORT
# =====================================================================


def export_checkpoint(ledger: VerbaLedger) -> LedgerCheckpoint:
    """The ledger's current tip, via VerbaLedger.current_checkpoint().

    An empty ledger has no tip to anchor, so it raises CheckpointError.
    """

    checkpoint = ledger.current_checkpoint()

    if checkpoint is None:
        raise CheckpointError(
            "The ledger is empty: there is no checkpoint to export."
        )

    return checkpoint


def checkpoint_to_dict(checkpoint: LedgerCheckpoint) -> dict[str, Any]:
    return {
        "sequence": checkpoint.sequence,
        "entry_hash": checkpoint.entry_hash,
        "checked_at": checkpoint.checked_at,
    }


def save_checkpoint(
    checkpoint: LedgerCheckpoint,
    path: str | os.PathLike[str],
) -> Path:
    """Write a checkpoint file. Never overwrites an existing file.

    Overwriting an anchor could erase the very evidence of truncation
    it exists to provide, so an existing path is an error.
    """

    target = Path(path)

    blob = json.dumps(checkpoint_to_dict(checkpoint), sort_keys=True)

    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(blob + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    except FileExistsError:
        raise CheckpointError(
            f"Checkpoint file already exists, refusing to "
            f"overwrite: {target}"
        ) from None

    except OSError as exc:
        raise CheckpointError(
            f"Checkpoint file could not be written: {target} "
            f"({exc.strerror or 'I/O error'})"
        ) from None

    return target


# =====================================================================
# LOAD
# =====================================================================


def checkpoint_from_dict(raw: Any) -> LedgerCheckpoint:
    """Strictly validate and build a checkpoint."""

    if not isinstance(raw, dict):
        raise CheckpointError("Checkpoint must be a JSON object.")

    keys = set(raw)

    if keys != _FIELDS:
        missing = sorted(_FIELDS - keys)
        extra = sorted(keys - _FIELDS)

        problems = []

        if missing:
            problems.append(f"missing {missing}")

        if extra:
            problems.append(f"unexpected {extra}")

        raise CheckpointError(
            "Checkpoint has the wrong fields: "
            + ", ".join(problems)
            + "."
        )

    sequence = raw["sequence"]

    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
    ):
        raise CheckpointError(
            "Checkpoint sequence must be a non-negative integer."
        )

    entry_hash = raw["entry_hash"]

    if not isinstance(entry_hash, str) or not _HASH.match(entry_hash):
        raise CheckpointError(
            "Checkpoint entry_hash must be 64 lowercase hex "
            "characters."
        )

    checked_at = raw["checked_at"]

    if (
        not isinstance(checked_at, (int, float))
        or isinstance(checked_at, bool)
        or not math.isfinite(checked_at)
    ):
        raise CheckpointError(
            "Checkpoint checked_at must be a finite number."
        )

    return LedgerCheckpoint(
        sequence=sequence,
        entry_hash=entry_hash,
        checked_at=float(checked_at),
    )


def load_checkpoint(path: str | os.PathLike[str]) -> LedgerCheckpoint:
    """Read and strictly validate a checkpoint file."""

    source = Path(path)

    if not source.exists():
        raise CheckpointError(f"Checkpoint file not found: {source}")

    if not source.is_file():
        raise CheckpointError(
            f"Checkpoint path is not a file: {source}"
        )

    try:
        text = source.read_bytes().decode("utf-8")

    except UnicodeDecodeError:
        raise CheckpointError(
            f"Checkpoint file is not valid UTF-8: {source}"
        ) from None

    except OSError as exc:
        raise CheckpointError(
            f"Checkpoint file could not be read: {source} "
            f"({exc.strerror or 'I/O error'})"
        ) from None

    try:
        raw = json.loads(text)

    except json.JSONDecodeError:
        raise CheckpointError(
            f"Checkpoint file is not valid JSON: {source}"
        ) from None

    return checkpoint_from_dict(raw)


# =====================================================================
# VERIFY
# =====================================================================


def verify_checkpoint(
    ledger: VerbaLedger,
    checkpoint: LedgerCheckpoint,
) -> CheckpointVerification:
    """Compare a ledger against an anchored checkpoint."""

    entries = list(ledger.store.all_entries())

    anchored = checkpoint.sequence

    if not entries:
        return CheckpointVerification(
            ok=False,
            code=TRUNCATED,
            detail=(
                "The ledger has no entries but the checkpoint "
                f"anchors sequence {anchored}."
            ),
            anchored_sequence=anchored,
            ledger_tip_sequence=None,
        )

    tip = entries[-1].sequence

    if tip < anchored:
        return CheckpointVerification(
            ok=False,
            code=TRUNCATED,
            detail=(
                f"The ledger ends at sequence {tip}, below the "
                f"anchored sequence {anchored}."
            ),
            anchored_sequence=anchored,
            ledger_tip_sequence=tip,
        )

    at_anchor = next(
        (e for e in entries if e.sequence == anchored),
        None,
    )

    if at_anchor is None:
        return CheckpointVerification(
            ok=False,
            code=MISSING_ENTRY,
            detail=(
                f"The ledger has no entry at the anchored "
                f"sequence {anchored}."
            ),
            anchored_sequence=anchored,
            ledger_tip_sequence=tip,
        )

    if at_anchor.entry_hash != checkpoint.entry_hash:
        return CheckpointVerification(
            ok=False,
            code=MISMATCH,
            detail=(
                f"The entry at sequence {anchored} has a different "
                "hash from the checkpoint: the chain was altered or "
                "replaced."
            ),
            anchored_sequence=anchored,
            ledger_tip_sequence=tip,
        )

    return CheckpointVerification(
        ok=True,
        code=OK,
        detail=(
            f"The entry at sequence {anchored} matches the "
            "checkpoint."
        ),
        anchored_sequence=anchored,
        ledger_tip_sequence=tip,
    )
