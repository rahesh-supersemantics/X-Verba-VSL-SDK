"""Shared helpers for the Phase 4 ledger, checkpoint and CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

from vsl import Governance
from vsl_core.governance import GovernanceAuthority
from vsl_core.identity import Evidence
from vsl_core.ledger import LedgerEntryType

AGENT = "expense-agent"


def ok_ctx(confidence: float = 0.95, amount: float = 150) -> dict:
    return {"confidence": confidence, "amount": amount, "currency": "NZD"}


def over_cap_ctx() -> dict:
    return ok_ctx(amount=5000)


def entries(governance: Governance) -> list:
    return list(governance.ledger.store.all_entries())


def ledger_file(governance: Governance) -> Path:
    return governance.ledger.store.path


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def edit_line(path: Path, index: int, **changes) -> None:
    """Rewrite one JSONL line's top-level fields (no hash recompute)."""

    lines = read_lines(path)
    raw = json.loads(lines[index])
    raw.update(changes)
    lines[index] = json.dumps(raw, sort_keys=True)
    write_lines(path, lines)


def edit_payload(path: Path, index: int, **changes) -> None:
    lines = read_lines(path)
    raw = json.loads(lines[index])
    raw["payload"].update(changes)
    lines[index] = json.dumps(raw, sort_keys=True)
    write_lines(path, lines)


def clean_traffic(governance: Governance) -> None:
    """Proceed + ordinary PreNode denial: 6 clean entries."""

    import asyncio

    asyncio.run(governance.authorize("approve_expense", ok_ctx()))
    asyncio.run(
        governance.authorize("approve_expense", ok_ctx(confidence=0.51))
    )


def suspend(governance: Governance) -> None:
    import asyncio

    asyncio.run(governance.authorize("approve_expense", over_cap_ctx()))


def resolve(governance: Governance) -> None:
    insufficient = next(
        e
        for e in entries(governance)
        if e.entry_type == LedgerEntryType.VERIFICATION
        and e.payload["result"] == "INSUFFICIENT"
    )

    governance.reenable(
        authority=GovernanceAuthority(
            name="Finance Controls",
            escalation_contact="controls@example.com",
        ),
        authorised_by="j.doe",
        evidence=Evidence(
            root_cause_analysis="OCR misread the amount.",
            specification_update_proposal="Add an OCR-confidence PreNode.",
        ),
    )

    governance.record_specification_update(
        verification_entry_id=insufficient.entry_id,
        new_policy_version="1.1.0",
        summary="Added OCR-confidence PreNode",
        approved_by="Finance Controls",
    )
