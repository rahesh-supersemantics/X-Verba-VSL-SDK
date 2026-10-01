"""
Phase 4 — checkpoint export and verification.

Demonstrates the known limitation and its mitigation:

    Ledger -> Checkpoint -> truncation
        verify_integrity() still says True      (a short chain is valid)
        verify_checkpoint() reports the mismatch
"""

from __future__ import annotations

import json
import math

import pytest

from vsl import (
    CheckpointError,
    CheckpointVerification,
    Governance,
    export_checkpoint,
    jsonl_ledger,
    load_checkpoint,
    memory_ledger,
    open_jsonl_ledger,
    save_checkpoint,
    verify_checkpoint,
)
from vsl.checkpoint import (
    MISMATCH,
    MISSING_ENTRY,
    OK,
    TRUNCATED,
    checkpoint_from_dict,
    checkpoint_to_dict,
)

from ledger_helpers import (
    AGENT,
    clean_traffic,
    edit_payload,
    ledger_file,
    read_lines,
    write_lines,
)
from vsl_core.ledger import LedgerCheckpoint


def monitors(ledger, n: int, tag: str = "a"):
    for i in range(n):
        ledger.write_monitor(
            identity_key=AGENT,
            instance_id="i",
            drift_detected=False,
            extra_payload={"n": i, "tag": tag},
        )


@pytest.fixture
def ledger(tmp_path):
    ledger = jsonl_ledger(tmp_path / "l.jsonl")
    monitors(ledger, 6)
    return ledger


def path_of(ledger):
    return ledger.store.path


# ================================================================
# EXPORT
# ================================================================


def test_export_uses_the_vsl_core_checkpoint(ledger):
    checkpoint = export_checkpoint(ledger)
    tip = list(ledger.store.all_entries())[-1]

    assert isinstance(checkpoint, LedgerCheckpoint)
    assert checkpoint.sequence == 5
    assert checkpoint.entry_hash == tip.entry_hash


def test_export_of_an_empty_ledger_is_an_error():
    with pytest.raises(CheckpointError, match="empty"):
        export_checkpoint(memory_ledger())


def test_checkpoint_dict_has_exactly_the_three_vsl_core_fields(ledger):
    assert set(checkpoint_to_dict(export_checkpoint(ledger))) == {
        "sequence",
        "entry_hash",
        "checked_at",
    }


def test_governance_checkpoint_matches_the_ledger(governance):
    assert governance.checkpoint() is None

    clean_traffic(governance)

    assert governance.checkpoint().sequence == 5


# ================================================================
# SAVE / LOAD
# ================================================================


def test_save_and_load_round_trip(ledger, tmp_path):
    original = export_checkpoint(ledger)
    target = tmp_path / "anchor.json"

    save_checkpoint(original, target)

    assert load_checkpoint(target) == original


def test_saved_file_is_the_documented_json(ledger, tmp_path):
    target = tmp_path / "anchor.json"

    save_checkpoint(export_checkpoint(ledger), target)

    raw = json.loads(target.read_text())

    assert set(raw) == {"sequence", "entry_hash", "checked_at"}
    assert len(raw["entry_hash"]) == 64


def test_save_never_overwrites_an_existing_checkpoint(ledger, tmp_path):
    target = tmp_path / "anchor.json"
    first = export_checkpoint(ledger)
    save_checkpoint(first, target)

    monitors(ledger, 2)

    with pytest.raises(CheckpointError, match="already exists"):
        save_checkpoint(export_checkpoint(ledger), target)

    assert load_checkpoint(target) == first


def test_save_into_a_missing_directory_is_an_error(ledger, tmp_path):
    with pytest.raises(CheckpointError, match="could not be written"):
        save_checkpoint(export_checkpoint(ledger), tmp_path / "no" / "anchor.json")


def test_load_missing_file_is_an_error(tmp_path):
    with pytest.raises(CheckpointError, match="not found"):
        load_checkpoint(tmp_path / "nope.json")


def test_load_directory_is_an_error(tmp_path):
    with pytest.raises(CheckpointError, match="not a file"):
        load_checkpoint(tmp_path)


def test_load_non_json_is_an_error(tmp_path):
    target = tmp_path / "a.json"
    target.write_text("not json")

    with pytest.raises(CheckpointError, match="not valid JSON"):
        load_checkpoint(target)


def test_load_non_utf8_is_an_error(tmp_path):
    target = tmp_path / "a.json"
    target.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(CheckpointError, match="UTF-8"):
        load_checkpoint(target)


GOOD = {"sequence": 3, "entry_hash": "a" * 64, "checked_at": 1.5}


@pytest.mark.parametrize("raw", [[], "text", 5, None])
def test_non_object_checkpoint_is_invalid(raw):
    with pytest.raises(CheckpointError, match="JSON object"):
        checkpoint_from_dict(raw)


@pytest.mark.parametrize("field", ["sequence", "entry_hash", "checked_at"])
def test_missing_field_is_invalid(field):
    raw = dict(GOOD)
    del raw[field]

    with pytest.raises(CheckpointError, match=field):
        checkpoint_from_dict(raw)


def test_unexpected_field_is_invalid():
    with pytest.raises(CheckpointError, match="unexpected"):
        checkpoint_from_dict({**GOOD, "signed_by": "me"})


@pytest.mark.parametrize(
    "field, value",
    [
        ("sequence", -1),
        ("sequence", 1.5),
        ("sequence", "3"),
        ("sequence", True),
        ("sequence", None),
        ("entry_hash", "A" * 64),
        ("entry_hash", "a" * 63),
        ("entry_hash", "g" * 64),
        ("entry_hash", 5),
        ("checked_at", "now"),
        ("checked_at", True),
        ("checked_at", math.inf),
        ("checked_at", math.nan),
    ],
)
def test_invalid_field_values_are_rejected(field, value):
    with pytest.raises(CheckpointError):
        checkpoint_from_dict({**GOOD, field: value})


def test_non_finite_numbers_in_a_file_are_rejected(tmp_path):
    target = tmp_path / "a.json"
    target.write_text(
        '{"sequence": 3, "entry_hash": "%s", "checked_at": NaN}' % ("a" * 64)
    )

    with pytest.raises(CheckpointError, match="checked_at"):
        load_checkpoint(target)


# ================================================================
# VERIFY: VALID
# ================================================================


def test_unchanged_ledger_matches_its_checkpoint(ledger):
    result = verify_checkpoint(ledger, export_checkpoint(ledger))

    assert isinstance(result, CheckpointVerification)
    assert result.ok and result.code == OK
    assert result.anchored_sequence == 5
    assert result.ledger_tip_sequence == 5


def test_a_ledger_that_grew_since_the_checkpoint_still_matches(ledger):
    anchored = export_checkpoint(ledger)

    monitors(ledger, 4, tag="later")

    result = verify_checkpoint(ledger, anchored)

    assert result.ok
    assert result.ledger_tip_sequence == 9
    assert ledger.verify_integrity() is True


def test_checkpoint_survives_reopening_the_file(ledger):
    anchored = export_checkpoint(ledger)

    reopened = open_jsonl_ledger(path_of(ledger))

    assert verify_checkpoint(reopened, anchored).ok


def test_governance_verifies_its_own_checkpoint(governance):
    clean_traffic(governance)
    anchored = governance.checkpoint()

    assert governance.verify_checkpoint(anchored).ok


# ================================================================
# VERIFY: TRUNCATION  (the known limitation and its mitigation)
# ================================================================


@pytest.mark.parametrize("keep", [5, 3, 1])
def test_truncation_is_invisible_to_verify_integrity_but_caught_by_checkpoint(
    ledger, keep
):
    anchored = export_checkpoint(ledger)

    write_lines(path_of(ledger), read_lines(path_of(ledger))[:keep])

    truncated = open_jsonl_ledger(path_of(ledger))

    # The limitation: a shorter chain is still a valid chain.
    assert truncated.verify_integrity() is True

    # The mitigation: the anchor reveals it.
    result = verify_checkpoint(truncated, anchored)

    assert not result.ok
    assert result.code == TRUNCATED
    assert result.ledger_tip_sequence == keep - 1
    assert result.anchored_sequence == 5


def test_truncation_to_an_empty_ledger_is_caught(ledger):
    anchored = export_checkpoint(ledger)

    path_of(ledger).write_text("")

    emptied = open_jsonl_ledger(path_of(ledger))

    assert emptied.verify_integrity() is True

    result = verify_checkpoint(emptied, anchored)

    assert result.code == TRUNCATED
    assert result.ledger_tip_sequence is None


def test_truncation_then_appending_new_entries_is_still_caught(ledger):
    """Cut the tail, then keep writing so the ledger is long again."""

    anchored = export_checkpoint(ledger)

    write_lines(path_of(ledger), read_lines(path_of(ledger))[:3])

    rewritten = jsonl_ledger(path_of(ledger))
    monitors(rewritten, 5, tag="replacement")

    assert rewritten.verify_integrity() is True

    result = verify_checkpoint(rewritten, anchored)

    assert result.code == MISMATCH


# ================================================================
# VERIFY: ALTERATION, MISSING ENTRIES, MISMATCH
# ================================================================


def test_edited_payload_is_caught_by_integrity_not_by_the_anchor(ledger):
    """The anchor compares the stored hash at one sequence. An edit to
    an earlier entry is verify_integrity()'s job: run both."""

    anchored = export_checkpoint(ledger)

    edit_payload(path_of(ledger), 2, tag="forged")

    tampered = open_jsonl_ledger(path_of(ledger))

    assert tampered.verify_integrity() is False
    assert verify_checkpoint(tampered, anchored).ok


def test_edited_anchored_entry_is_caught_by_integrity(ledger):
    anchored = export_checkpoint(ledger)

    edit_payload(path_of(ledger), 5, tag="forged")

    tampered = open_jsonl_ledger(path_of(ledger))

    assert tampered.verify_integrity() is False


def test_rewritten_chain_with_valid_hashes_is_caught_only_by_the_anchor(
    ledger, tmp_path
):
    """A forger who rebuilds the whole chain produces a ledger that
    verify_integrity() accepts. Only the external anchor exposes it."""

    anchored = export_checkpoint(ledger)

    forged = jsonl_ledger(tmp_path / "forged.jsonl")
    monitors(forged, 6, tag="forged")

    assert forged.verify_integrity() is True
    assert forged.current_checkpoint().sequence == anchored.sequence

    result = verify_checkpoint(forged, anchored)

    assert not result.ok
    assert result.code == MISMATCH


def test_missing_anchored_entry_is_reported(ledger):
    monitors(ledger, 0)
    full = export_checkpoint(ledger)
    anchored = LedgerCheckpoint(
        sequence=2,
        entry_hash=list(ledger.store.all_entries())[2].entry_hash,
        checked_at=full.checked_at,
    )

    lines = read_lines(path_of(ledger))
    del lines[2]
    write_lines(path_of(ledger), lines)

    damaged = open_jsonl_ledger(path_of(ledger))

    assert damaged.verify_integrity() is False

    result = verify_checkpoint(damaged, anchored)

    assert result.code == MISSING_ENTRY
    assert result.ledger_tip_sequence == 5


def test_a_missing_middle_entry_breaks_integrity_even_if_the_tip_matches(ledger):
    anchored = export_checkpoint(ledger)

    lines = read_lines(path_of(ledger))
    del lines[2]
    write_lines(path_of(ledger), lines)

    damaged = open_jsonl_ledger(path_of(ledger))

    assert damaged.verify_integrity() is False


def test_checkpoint_from_a_different_ledger_is_a_mismatch(ledger, tmp_path):
    other = jsonl_ledger(tmp_path / "other.jsonl")
    monitors(other, 6, tag="other")

    result = verify_checkpoint(ledger, export_checkpoint(other))

    assert result.code == MISMATCH


def test_checkpoint_with_a_wrong_hash_is_a_mismatch(ledger):
    real = export_checkpoint(ledger)

    wrong = LedgerCheckpoint(
        sequence=real.sequence,
        entry_hash="0" * 64,
        checked_at=real.checked_at,
    )

    result = verify_checkpoint(ledger, wrong)

    assert not result.ok and result.code == MISMATCH


def test_checkpoint_beyond_the_ledger_is_reported_as_truncation(ledger):
    beyond = LedgerCheckpoint(sequence=99, entry_hash="a" * 64, checked_at=0.0)

    assert verify_checkpoint(ledger, beyond).code == TRUNCATED


def test_every_failure_has_a_human_readable_detail(ledger):
    beyond = LedgerCheckpoint(sequence=99, entry_hash="a" * 64, checked_at=0.0)

    assert "99" in verify_checkpoint(ledger, beyond).detail
