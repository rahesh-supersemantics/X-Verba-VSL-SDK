"""Ledger integration helpers.

A thin layer that selects and opens a VSL-Core ledger store. It does not
implement hashing, chaining, sequencing or any ledger semantics: those
belong to vsl_core.ledger (VerbaLedger and its stores) and, for the
hosted backend, vsl_core_ledger_client.RemoteLedgerStore.

Backends
--------
memory   vsl_core.ledger.InMemoryLedgerStore
jsonl    vsl_core.ledger.JsonlLedgerStore
remote   vsl_core_ledger_client.RemoteLedgerStore (optional package)

Actual guarantees (from the pinned VSL-Core and ledger-client sources):

- JSONL: one JSON object per line, append-only writes. Writers hold a
  cross-process file lock around read-last-entry-then-append. The
  store re-reads the whole file on every append and on every
  last_entry(), so cost grows with ledger size. Anyone with write
  access to the file can still edit or truncate it: the hash chain makes
  edits detectable, not impossible.
- memory: process-local, lost on exit. Same VerbaLedger semantics.
- remote: HTTP client for the hosted ledger-api. last_entry() walks all
  entries. Any non-2xx response raises RemoteLedgerStoreError, which the
  SDK does not swallow (a failed write fails the governed action
  closed).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from vsl_core.ledger import (
    InMemoryLedgerStore,
    JsonlLedgerStore,
    LedgerEntry,
    VerbaLedger,
)

from .exceptions import LedgerStorageError

BACKENDS = ("jsonl", "remote", "memory")

ENV_LEDGER_URL = "LEDGER_API_URL"
ENV_LEDGER_KEY = "LEDGER_API_KEY"


@dataclass(frozen=True)
class LedgerConfig:
    """The validated `ledger:` section of a policy."""

    backend: str
    path: str | None = None
    fsync: bool = True


# =====================================================================
# JSONL
# =====================================================================


def _scan_jsonl(path: Path) -> int:
    """Strictly parse a JSONL ledger file; return the entry count.

    This only checks that every line is a well-formed ledger entry so
    problems surface as a clear LedgerStorageError instead of a raw
    JSON/KeyError deep inside a store. It does NOT recompute hashes:
    integrity is VerbaLedger.verify_integrity()'s job.
    """

    try:
        text = path.read_bytes().decode("utf-8")

    except UnicodeDecodeError:
        raise LedgerStorageError(
            f"Ledger file is not valid UTF-8: {path}"
        ) from None

    except OSError as exc:
        raise LedgerStorageError(
            f"Ledger file could not be read: {path} "
            f"({exc.strerror or 'I/O error'})"
        ) from None

    count = 0

    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()

        if not line:
            continue

        try:
            raw = json.loads(line)

        except json.JSONDecodeError:
            raise LedgerStorageError(
                f"Ledger {path}: line {number} is not valid JSON."
            ) from None

        if not isinstance(raw, dict):
            raise LedgerStorageError(
                f"Ledger {path}: line {number} is not a ledger "
                "entry object."
            )

        try:
            LedgerEntry.from_dict(raw)

        except (KeyError, ValueError, TypeError):
            raise LedgerStorageError(
                f"Ledger {path}: line {number} is not a valid "
                "ledger entry."
            ) from None

        count += 1

    return count


def open_jsonl_ledger(
    path: str | os.PathLike[str],
) -> VerbaLedger:
    """Open an EXISTING JSONL ledger for reading and inspection.

    Never creates the file: JsonlLedgerStore would silently create an
    empty one for a mistyped path, and an empty ledger verifies as
    clean. A missing file is an error here.
    """

    file_path = Path(path)

    if not file_path.exists():
        raise LedgerStorageError(f"Ledger file not found: {file_path}")

    if not file_path.is_file():
        raise LedgerStorageError(
            f"Ledger path is not a file: {file_path}"
        )

    _scan_jsonl(file_path)

    return VerbaLedger(JsonlLedgerStore(file_path))


def jsonl_ledger(
    path: str | os.PathLike[str],
    *,
    fsync: bool = True,
    create_parents: bool = True,
) -> VerbaLedger:
    """Open or create a JSONL ledger for governed writes.

    fsync defaults to True: a record lost in a page cache never
    happened. An existing file is parsed first, so a corrupt ledger is
    refused up front rather than failing mid-request.
    """

    file_path = Path(path)

    if file_path.exists():
        if not file_path.is_file():
            raise LedgerStorageError(
                f"Ledger path is not a file: {file_path}"
            )

        _scan_jsonl(file_path)

    else:
        try:
            if create_parents:
                file_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LedgerStorageError(
                f"Ledger directory could not be created: "
                f"{file_path.parent} ({exc.strerror or 'I/O error'})"
            ) from None

        if not file_path.parent.is_dir():
            raise LedgerStorageError(
                f"Ledger directory does not exist: {file_path.parent}"
            )

    try:
        return VerbaLedger(JsonlLedgerStore(file_path, fsync=fsync))

    except OSError as exc:
        raise LedgerStorageError(
            f"Ledger file could not be opened: {file_path} "
            f"({exc.strerror or 'I/O error'})"
        ) from None


# =====================================================================
# IN-MEMORY
# =====================================================================


def memory_ledger() -> VerbaLedger:
    """A process-local ledger for tests and development."""

    return VerbaLedger(InMemoryLedgerStore())


# =====================================================================
# REMOTE
# =====================================================================


def remote_ledger(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    env: Mapping[str, str] | None = None,
    client: Any = None,
    timeout_seconds: float | None = None,
    page_size: int | None = None,
) -> VerbaLedger:
    """A VerbaLedger over vsl_core_ledger_client.RemoteLedgerStore.

    Credentials come from the arguments or, by default, from the
    LEDGER_API_URL and LEDGER_API_KEY environment variables. They are
    never read from a policy file. The URL must be https.

    `client` is an optional pre-built httpx.Client (the real
    RemoteLedgerStore parameter), used for tests and custom transports.
    """

    try:
        from vsl_core_ledger_client import RemoteLedgerStore

    except ImportError:
        raise LedgerStorageError(
            "The remote ledger backend needs the optional "
            "vsl-core-ledger-client package (and httpx). Install it "
            "from github.com/supersemantics/VSL-Core-ledger-client."
        ) from None

    source = os.environ if env is None else env

    url = base_url or source.get(ENV_LEDGER_URL)
    key = api_key or source.get(ENV_LEDGER_KEY)

    if not url:
        raise LedgerStorageError(
            f"Remote ledger URL is not set ({ENV_LEDGER_URL})."
        )

    if not key:
        raise LedgerStorageError(
            f"Remote ledger API key is not set ({ENV_LEDGER_KEY})."
        )

    if not url.lower().startswith("https://"):
        raise LedgerStorageError(
            "Remote ledger URL must use https."
        )

    kwargs: dict[str, Any] = {"base_url": url, "api_key": key}

    if client is not None:
        kwargs["client"] = client

    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds

    if page_size is not None:
        kwargs["page_size"] = page_size

    return VerbaLedger(store=RemoteLedgerStore(**kwargs))


# =====================================================================
# SELECTION
# =====================================================================


def build_ledger(
    config: LedgerConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> VerbaLedger:
    """Build the ledger a policy's `ledger:` section describes."""

    if config.backend == "memory":
        return memory_ledger()

    if config.backend == "jsonl":
        if not config.path:
            raise LedgerStorageError(
                "jsonl ledger requires a path."
            )

        return jsonl_ledger(config.path, fsync=config.fsync)

    if config.backend == "remote":
        return remote_ledger(env=env)

    raise LedgerStorageError(
        f"Unsupported ledger backend {config.backend!r}. "
        f"Supported: {BACKENDS}."
    )
