"""A reusable contract every X-Verba framework adapter must satisfy.

Subclass :class:`AdapterContract` in a test module, implement
:meth:`invoke`, and pytest collects one test per guarantee. The contract
is framework-neutral: it states what must be true when an application
runs a governed action *through an adapter*, using only the public SDK
and observable side effects.

    class TestMyAdapter(AdapterContract):
        async def invoke(self, governance, action, context, effect):
            ...  # run the action through the adapter
            return Outcome.PROCEED  # what the application observed

``invoke`` must:
  * run ``effect`` only if governance returned PROCEED,
  * return the :class:`~vsl.Outcome` governance produced,
  * let exceptions raised by ``effect`` or by governance propagate
    unchanged (never turn them into a denial, never swallow them).

The contract is also run against ``Governance.run`` itself (the
"no-framework" adapter), which keeps the contract honest.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from vsl.outcome import Outcome

from .assertions import (
    assert_audit_passes,
    assert_causal_chain_valid,
    assert_decision_evidence,
    assert_integrity_ok,
    assert_not_suspended,
    assert_suspension_active,
)
from .harness import GovernedHarness, build_harness
from .ledger import latest_decision_id, ledger_entries
from .lifecycle import resolve_suspension, suspend
from .policies import (
    EXPENSE_ACTION,
    low_confidence_context,
    ok_context,
    over_cap_context,
)
from .side_effect import SideEffect


class _WrappedFailure:
    """Context manager: the block must raise ``wrapper`` caused by ``original``."""

    def __init__(self, wrapper: type[BaseException], original: type[BaseException], match: str):
        self._wrapper, self._original, self._match = wrapper, original, match
        self._inner = pytest.raises(wrapper)

    def __enter__(self) -> "_WrappedFailure":
        self._info = self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> bool:
        suppressed = self._inner.__exit__(*exc)

        if suppressed:
            cause = self._info.value.__cause__
            assert isinstance(cause, self._original), (
                f"expected cause {self._original.__name__}, got {cause!r}"
            )
            assert self._match in str(cause)

        return suppressed


class AdapterContract:
    """Subclass and implement :meth:`invoke`. Not collected on its own
    (the name does not start with ``Test``)."""

    # ---- declared properties of the adapter ------------------------

    #: ``True`` if an exception raised by the effect reaches the caller.
    #: Set ``False`` only for a framework that itself turns tool
    #: exceptions into tool-error results (Microsoft Agent Framework
    #: does); the adapter must then still never report that as a denial.
    effect_errors_propagate: bool = True

    #: ``None`` if governance errors (unknown action, ledger failure)
    #: reach the caller as raised. Otherwise the exception type the
    #: adapter wraps them in, with the original as ``__cause__``.
    governance_failure_wrapper: type[BaseException] | None = None

    # ---- to implement ----------------------------------------------

    async def invoke(
        self,
        governance: Any,
        action: str,
        context: dict[str, Any],
        effect: SideEffect,
    ) -> Outcome:
        raise NotImplementedError

    # ---- helpers ---------------------------------------------------

    def run_action(
        self,
        harness: GovernedHarness,
        context: dict[str, Any],
        effect: SideEffect,
        *,
        action: str = EXPENSE_ACTION,
    ) -> Outcome:
        return asyncio.run(
            self.invoke(harness.governance, action, context, effect)
        )

    def _raises_governance_failure(self, original: type[BaseException], match: str):
        """Expect the governance error, directly or inside the declared wrapper."""

        wrapper = self.governance_failure_wrapper

        if wrapper is None:
            return pytest.raises(original, match=match)

        return _WrappedFailure(wrapper, original, match)

    @staticmethod
    def _evidence_for_latest(harness: GovernedHarness, outcome: Outcome) -> list[Any]:
        """Assert the evidence for the most recent decision, read from the
        ledger (adapters do not hand the Decision back to the contract)."""

        governance = harness.governance
        seen = SimpleNamespace(
            decision_id=latest_decision_id(governance),
            outcome=outcome,
            terminal_state=None,
        )
        return assert_decision_evidence(governance, seen)

    # ---- PROCEED ---------------------------------------------------

    def test_proceed_executes_the_effect_once_after_governance_evidence(
        self, tmp_path: Any
    ) -> None:
        harness = build_harness(tmp_path)
        effect = harness.side_effect(result="done")

        outcome = self.run_action(harness, ok_context(), effect)

        assert outcome is Outcome.PROCEED
        effect.assert_executed(1)

        # Governance came first: when the effect ran, MONITOR, PRE_NODE
        # and a VERIFICATION were already in the ledger.
        assert effect.observations[0] == ["MONITOR", "PRE_NODE", "VERIFICATION"]

        self._evidence_for_latest(harness, Outcome.PROCEED)
        assert_integrity_ok(harness.governance)
        assert_causal_chain_valid(harness.governance)

    # ---- HUMAN_QUEUE -----------------------------------------------

    def test_human_queue_never_executes_the_effect(self, tmp_path: Any) -> None:
        harness = build_harness(tmp_path)
        effect = harness.side_effect()

        outcome = self.run_action(harness, low_confidence_context(), effect)

        assert outcome is Outcome.HUMAN_QUEUE
        effect.assert_not_executed("PreNode denial")

        self._evidence_for_latest(harness, Outcome.HUMAN_QUEUE)
        assert_not_suspended(harness.governance)  # denial is not a suspension

    # ---- SUSPENDED / INSUFFICIENT ----------------------------------

    def test_invariant_violation_suspends_and_blocks_the_effect(
        self, tmp_path: Any
    ) -> None:
        harness = build_harness(tmp_path)
        effect = harness.side_effect()

        outcome = self.run_action(harness, over_cap_context(), effect)

        assert outcome is Outcome.SUSPENDED
        effect.assert_not_executed("invariant violation")

        entries = self._evidence_for_latest(harness, Outcome.SUSPENDED)
        assert [e.entry_type.value for e in entries] == [
            "MONITOR",
            "PRE_NODE",
            "VERIFICATION",
            "TERMINAL",
        ]
        assert entries[2].payload["result"] == "INSUFFICIENT"
        assert_suspension_active(harness.governance)

    def test_suspension_blocks_every_later_action(self, tmp_path: Any) -> None:
        harness = build_harness(tmp_path)
        self.run_action(harness, over_cap_context(), harness.side_effect())

        effect = harness.side_effect()
        outcome = self.run_action(harness, ok_context(), effect)  # would PROCEED

        assert outcome is Outcome.SUSPENDED
        effect.assert_not_executed("active suspension")

        entries = self._evidence_for_latest(harness, Outcome.SUSPENDED)
        assert len(entries) == 1 and "suspended_by" in entries[0].payload

    def test_reenable_requires_the_human_flow_and_then_resumes(
        self, tmp_path: Any
    ) -> None:
        harness = build_harness(tmp_path)
        asyncio.run(suspend(harness.governance))

        # Nothing the application does through the adapter lifts the
        # suspension; only the human re-enablement flow does.
        blocked = harness.side_effect()
        assert self.run_action(harness, ok_context(), blocked) is Outcome.SUSPENDED
        blocked.assert_not_executed()

        resolve_suspension(harness.governance)
        assert_not_suspended(harness.governance)

        resumed = harness.side_effect()
        assert self.run_action(harness, ok_context(), resumed) is Outcome.PROCEED
        resumed.assert_executed(1)

        assert_integrity_ok(harness.governance)
        assert_audit_passes(harness.governance, max_monitor_gap_seconds=300)

    # ---- errors ----------------------------------------------------

    def test_effect_exception_propagates_and_is_not_a_denial(
        self, tmp_path: Any
    ) -> None:
        harness = build_harness(tmp_path)
        effect = harness.side_effect(raises=RuntimeError("downstream failed"))

        if self.effect_errors_propagate:
            with pytest.raises(RuntimeError, match="downstream failed"):
                self.run_action(harness, ok_context(), effect)
        else:
            # The framework absorbs the error; the effect still ran once
            # and governance still recorded PROCEED.
            self.run_action(harness, ok_context(), effect)

        effect.assert_executed(1)

        # Governance had permitted it, and the evidence says so.
        self._evidence_for_latest(harness, Outcome.PROCEED)
        assert_not_suspended(harness.governance)

    def test_unknown_action_raises_and_does_not_execute(self, tmp_path: Any) -> None:
        harness = build_harness(tmp_path)
        effect = harness.side_effect()

        with self._raises_governance_failure(ValueError, "Unknown action"):
            self.run_action(harness, ok_context(), effect, action="not_an_action")

        effect.assert_not_executed()
        assert ledger_entries(harness.governance) == []

    def test_ledger_failure_stops_the_effect_and_propagates(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        harness = build_harness(tmp_path)
        effect = harness.side_effect()

        def broken(*_: Any, **__: Any) -> None:
            raise OSError("ledger unavailable")

        monkeypatch.setattr(harness.governance.ledger, "write_monitor", broken)

        with self._raises_governance_failure(OSError, "ledger unavailable"):
            self.run_action(harness, ok_context(), effect)

        effect.assert_not_executed("ledger failure")

    def test_the_callers_context_is_not_mutated(self, tmp_path: Any) -> None:
        harness = build_harness(tmp_path)
        context = ok_context()
        snapshot = dict(context)

        self.run_action(harness, context, harness.side_effect())

        assert context == snapshot

    # ---- whole ledger ----------------------------------------------

    def test_mixed_traffic_leaves_a_verifiable_ledger(self, tmp_path: Any) -> None:
        harness = build_harness(tmp_path)

        self.run_action(harness, ok_context(), harness.side_effect())
        self.run_action(harness, low_confidence_context(), harness.side_effect())
        self.run_action(harness, over_cap_context(), harness.side_effect())
        resolve_suspension(harness.governance)
        self.run_action(harness, ok_context(), harness.side_effect())

        assert_integrity_ok(harness.governance)
        assert_causal_chain_valid(harness.governance)
        assert_audit_passes(harness.governance, max_monitor_gap_seconds=300)
