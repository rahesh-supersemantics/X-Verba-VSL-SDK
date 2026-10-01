from pathlib import Path

import pytest

from vsl import Governance
from vsl_core.ledger import JsonlLedgerStore, VerbaLedger

from governance.policy import build_policy


@pytest.fixture
def governance(tmp_path: Path):
    ledger_path = tmp_path / "ledger.jsonl"

    ledger = VerbaLedger(
        JsonlLedgerStore(
            ledger_path,
            fsync=True,
        )
    )

    policy = build_policy()

    return Governance(
        policy=policy,
        ledger=ledger,
    )