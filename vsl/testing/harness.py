"""A ready-made governed environment for tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from vsl.governance import Governance
from vsl.ledger import jsonl_ledger, memory_ledger

from .ledger import entries_for_decision, ledger_entries
from .policies import EXPENSE_POLICY_YAML, write_policy
from .side_effect import AsyncSideEffect, SideEffect


@dataclass
class GovernedHarness:
    """A real ``Governance`` plus the pieces tests keep reaching for."""

    governance: Governance
    policy_path: Path
    ledger_path: Path | None  # None for an in-memory ledger

    # -- governance --------------------------------------------------

    async def authorize(self, action: str, context: dict[str, Any], **kw: Any) -> Any:
        return await self.governance.authorize(action, context, **kw)

    async def run(
        self, action: str, context: dict[str, Any], effect: Callable[[], Any], **kw: Any
    ) -> Any:
        return await self.governance.run(action, context, effect, **kw)

    # -- evidence ----------------------------------------------------

    def entries(self) -> list[Any]:
        return ledger_entries(self.governance)

    def entries_for(self, decision: Any) -> list[Any]:
        return entries_for_decision(self.governance, decision.decision_id)

    # -- side effects ------------------------------------------------

    def _entry_types_now(self) -> list[str]:
        return [e.entry_type.value for e in self.entries()]

    def side_effect(self, result: Any = None, **kw: Any) -> SideEffect:
        """A recording effect that snapshots the ledger entry types at the
        moment it runs, proving what evidence existed before it."""

        kw.setdefault("observe", self._entry_types_now)
        return SideEffect(result, **kw)

    def async_side_effect(self, result: Any = None, **kw: Any) -> AsyncSideEffect:
        kw.setdefault("observe", self._entry_types_now)
        return AsyncSideEffect(result, **kw)


def build_harness(
    directory: str | Path,
    *,
    policy_text: str = EXPENSE_POLICY_YAML,
    ledger: str = "jsonl",
    env: Mapping[str, str] | None = None,
) -> GovernedHarness:
    """Build a harness from a YAML policy.

    ``directory``  where the policy file (and a JSONL ledger) are written;
                   pass pytest's ``tmp_path``.
    ``ledger``     ``"jsonl"`` (file-backed, can be tampered with) or
                   ``"memory"``.
    ``env``        environment used for ``${VAR}`` interpolation. Defaults
                   to an empty mapping so tests never depend on the real
                   environment.
    """

    if ledger not in ("jsonl", "memory"):
        raise ValueError("ledger must be 'jsonl' or 'memory'")

    directory = Path(directory)
    policy_path = write_policy(directory, policy_text)

    if ledger == "jsonl":
        ledger_path: Path | None = directory / "ledger.jsonl"
        built = jsonl_ledger(ledger_path)
    else:
        ledger_path = None
        built = memory_ledger()

    governance = Governance.from_yaml(
        policy_path,
        ledger=built,
        env={} if env is None else env,
    )

    return GovernedHarness(governance, policy_path, ledger_path)
