"""
Phase 4 — ledger backends: JSONL, in-memory, remote.

JSONL and in-memory use the real VSL-Core stores. The remote backend
uses the real vsl_core_ledger_client.RemoteLedgerStore over an httpx
MockTransport that fronts a real VSL-Core InMemoryLedgerStore, so
hash-chain sealing is genuine; only the network is faked.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from vsl import (
    Governance,
    LedgerConfig,
    LedgerStorageError,
    build_ledger,
    jsonl_ledger,
    memory_ledger,
    open_jsonl_ledger,
)

from governance.policy import build_policy
from ledger_helpers import (
    AGENT,
    clean_traffic,
    edit_line,
    entries,
    ledger_file,
    ok_ctx,
    read_lines,
    write_lines,
)
from vsl_core.ledger import (
    GENESIS_HASH,
    InMemoryLedgerStore,
    JsonlLedgerStore,
    LedgerEntry,
    LedgerEntryType,
    VerbaLedger,
)


def monitor(ledger: VerbaLedger, n: int = 1):
    for _ in range(n):
        ledger.write_monitor(
            identity_key=AGENT,
            instance_id="i",
            drift_detected=False,
        )


# ================================================================
# JSONL
# ================================================================


def test_jsonl_creates_a_new_ledger_and_parent_directories(tmp_path):
    path = tmp_path / "deep" / "er" / "ledger.jsonl"

    ledger = jsonl_ledger(path)

    assert path.is_file()
    assert path.read_text() == ""
    assert isinstance(ledger.store, JsonlLedgerStore)
    assert ledger.verify_integrity() is True


def test_jsonl_fsync_defaults_to_true_and_is_configurable(tmp_path):
    assert jsonl_ledger(tmp_path / "a.jsonl").store.fsync is True
    assert jsonl_ledger(tmp_path / "b.jsonl", fsync=False).store.fsync is False


def test_jsonl_appends_entries_as_one_line_each(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = jsonl_ledger(path)

    monitor(ledger, 3)

    lines = read_lines(path)

    assert len(lines) == 3
    assert [json.loads(line)["sequence"] for line in lines] == [0, 1, 2]


def test_jsonl_chain_starts_at_genesis_and_links(tmp_path):
    ledger = jsonl_ledger(tmp_path / "l.jsonl")

    monitor(ledger, 3)

    all_entries = list(ledger.store.all_entries())

    assert all_entries[0].prev_hash == GENESIS_HASH
    assert all_entries[1].prev_hash == all_entries[0].entry_hash
    assert all_entries[2].prev_hash == all_entries[1].entry_hash
    assert ledger.verify_integrity() is True


def test_jsonl_reopening_continues_the_same_chain(tmp_path):
    path = tmp_path / "l.jsonl"

    first = jsonl_ledger(path)
    monitor(first, 2)
    tip = first.current_checkpoint()

    second = jsonl_ledger(path)
    monitor(second, 1)

    all_entries = list(second.store.all_entries())

    assert [e.sequence for e in all_entries] == [0, 1, 2]
    assert all_entries[2].prev_hash == tip.entry_hash
    assert second.verify_integrity() is True


def test_jsonl_reopened_ledger_reads_back_every_entry(tmp_path):
    path = tmp_path / "l.jsonl"
    original = jsonl_ledger(path)
    monitor(original, 3)

    reopened = open_jsonl_ledger(path)

    assert [e.entry_id for e in reopened.store.all_entries()] == [
        e.entry_id for e in original.store.all_entries()
    ]


def test_jsonl_tolerates_blank_lines(tmp_path):
    path = tmp_path / "l.jsonl"
    monitor(jsonl_ledger(path), 2)

    lines = read_lines(path)
    write_lines(path, [lines[0], "", "   ", lines[1]])

    assert open_jsonl_ledger(path).verify_integrity() is True


@pytest.mark.parametrize("opener", [open_jsonl_ledger, jsonl_ledger])
def test_malformed_json_line_is_a_storage_error_naming_the_line(tmp_path, opener):
    path = tmp_path / "l.jsonl"
    monitor(jsonl_ledger(path), 2)

    lines = read_lines(path)
    write_lines(path, [lines[0], "{this is not json", lines[1]])

    with pytest.raises(LedgerStorageError, match="line 2 is not valid JSON"):
        opener(path)


@pytest.mark.parametrize("opener", [open_jsonl_ledger, jsonl_ledger])
@pytest.mark.parametrize("line", ["[1, 2, 3]", '"text"', "42", "null"])
def test_non_object_line_is_a_storage_error(tmp_path, opener, line):
    path = tmp_path / "l.jsonl"
    path.write_text(line + "\n")

    with pytest.raises(LedgerStorageError, match="not a ledger entry object"):
        opener(path)


@pytest.mark.parametrize("opener", [open_jsonl_ledger, jsonl_ledger])
@pytest.mark.parametrize(
    "field", ["entry_id", "sequence", "entry_type", "entry_hash", "prev_hash", "timestamp"]
)
def test_entry_missing_a_required_field_is_a_storage_error(tmp_path, opener, field):
    path = tmp_path / "l.jsonl"
    monitor(jsonl_ledger(path), 1)

    raw = json.loads(read_lines(path)[0])
    del raw[field]
    write_lines(path, [json.dumps(raw)])

    with pytest.raises(LedgerStorageError, match="not a valid ledger entry"):
        opener(path)


def test_unknown_entry_type_is_a_storage_error(tmp_path):
    path = tmp_path / "l.jsonl"
    monitor(jsonl_ledger(path), 1)
    edit_line(path, 0, entry_type="INVOKE_EVERYTHING")

    with pytest.raises(LedgerStorageError, match="not a valid ledger entry"):
        open_jsonl_ledger(path)


def test_non_utf8_ledger_is_a_storage_error(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(LedgerStorageError, match="UTF-8"):
        open_jsonl_ledger(path)


def test_storage_errors_do_not_echo_ledger_content(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text('{"secret": "s3cret-value", broken\n')

    with pytest.raises(LedgerStorageError) as info:
        open_jsonl_ledger(path)

    assert "s3cret-value" not in str(info.value)


def test_open_jsonl_ledger_never_creates_a_missing_file(tmp_path):
    path = tmp_path / "typo.jsonl"

    with pytest.raises(LedgerStorageError, match="not found"):
        open_jsonl_ledger(path)

    assert not path.exists()


def test_directory_is_not_a_ledger(tmp_path):
    for opener in (open_jsonl_ledger, jsonl_ledger):
        with pytest.raises(LedgerStorageError, match="not a file"):
            opener(tmp_path)


def test_missing_parent_without_create_parents_is_an_error(tmp_path):
    path = tmp_path / "missing" / "l.jsonl"

    with pytest.raises(LedgerStorageError, match="does not exist"):
        jsonl_ledger(path, create_parents=False)

    assert not path.parent.exists()


def test_parent_that_is_a_file_is_an_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")

    with pytest.raises(LedgerStorageError):
        jsonl_ledger(blocker / "l.jsonl")


def test_a_corrupt_existing_ledger_is_refused_before_any_write(tmp_path):
    path = tmp_path / "l.jsonl"
    monitor(jsonl_ledger(path), 1)
    before = path.read_bytes()

    path.write_bytes(before + b"garbage\n")
    corrupted = path.read_bytes()

    with pytest.raises(LedgerStorageError):
        jsonl_ledger(path)

    assert path.read_bytes() == corrupted


def test_sequence_continuity_is_checked_by_verify_not_by_open(tmp_path):
    """Opening only checks each line is a well-formed entry. A gap is
    an integrity problem, reported by VSL-Core's verify_integrity()."""

    path = tmp_path / "l.jsonl"
    monitor(jsonl_ledger(path), 3)

    lines = read_lines(path)
    write_lines(path, [lines[0], lines[2]])

    reopened = open_jsonl_ledger(path)

    assert reopened.verify_integrity() is False


def test_writing_after_tampering_is_refused_by_vsl_core(tmp_path):
    from vsl_core.exceptions import LedgerIntegrityError

    path = tmp_path / "l.jsonl"
    ledger = jsonl_ledger(path)
    monitor(ledger, 2)

    edit_line(path, 1, payload={"drift_detected": True})

    with pytest.raises(LedgerIntegrityError):
        monitor(jsonl_ledger(path), 1)


def test_concurrent_writers_with_separate_stores_keep_the_chain_valid(tmp_path):
    """Characterisation of VSL-Core's documented guarantee: appends from
    separate store instances on one file are serialised by a file lock.
    The SDK adds no guarantee beyond that."""

    path = tmp_path / "l.jsonl"
    jsonl_ledger(path)

    errors: list[BaseException] = []

    def worker():
        try:
            monitor(jsonl_ledger(path, fsync=False), 10)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    assert errors == []

    ledger = open_jsonl_ledger(path)

    assert [e.sequence for e in ledger.store.all_entries()] == list(range(40))
    assert ledger.verify_integrity() is True


# ================================================================
# IN-MEMORY
# ================================================================


def test_memory_ledger_uses_the_real_in_memory_store():
    ledger = memory_ledger()

    assert isinstance(ledger, VerbaLedger)
    assert isinstance(ledger.store, InMemoryLedgerStore)
    assert ledger.verify_integrity() is True


def test_memory_ledgers_are_independent():
    one, two = memory_ledger(), memory_ledger()

    monitor(one, 2)

    assert list(two.store.all_entries()) == []


def test_memory_ledger_has_the_same_chain_semantics_as_jsonl(tmp_path):
    memory = memory_ledger()
    persistent = jsonl_ledger(tmp_path / "l.jsonl")

    for ledger in (memory, persistent):
        monitor(ledger, 3)

    for ledger in (memory, persistent):
        chain = list(ledger.store.all_entries())

        assert [e.sequence for e in chain] == [0, 1, 2]
        assert chain[0].prev_hash == GENESIS_HASH
        assert chain[1].prev_hash == chain[0].entry_hash
        assert ledger.verify_integrity() is True


@pytest.mark.asyncio
async def test_governance_decisions_match_across_memory_and_jsonl(tmp_path):
    memory = Governance(policy=build_policy(), ledger=memory_ledger())
    jsonl = Governance(policy=build_policy(), ledger=jsonl_ledger(tmp_path / "l.jsonl"))

    for context in (ok_ctx(), ok_ctx(confidence=0.51), ok_ctx(amount=5000)):
        a = await memory.authorize("approve_expense", context)
        b = await jsonl.authorize("approve_expense", context)

        assert a.outcome is b.outcome

    assert [e.entry_type for e in entries(memory)] == [
        e.entry_type for e in entries(jsonl)
    ]


# ================================================================
# SELECTION (non-remote)
# ================================================================


def test_build_ledger_selects_memory_and_jsonl(tmp_path):
    assert isinstance(
        build_ledger(LedgerConfig("memory")).store, InMemoryLedgerStore
    )

    jsonl = build_ledger(
        LedgerConfig("jsonl", path=str(tmp_path / "l.jsonl"), fsync=False)
    )

    assert isinstance(jsonl.store, JsonlLedgerStore)
    assert jsonl.store.fsync is False


def test_build_ledger_rejects_unsupported_backend_and_missing_path():
    with pytest.raises(LedgerStorageError, match="Unsupported"):
        build_ledger(LedgerConfig("sqlite"))

    with pytest.raises(LedgerStorageError, match="requires a path"):
        build_ledger(LedgerConfig("jsonl"))
