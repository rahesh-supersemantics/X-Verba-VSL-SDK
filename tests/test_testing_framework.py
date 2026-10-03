"""
Phase 5 -- the testing framework tested on its own terms.

A testing framework is only worth shipping if it fails when it should.
Every assertion helper is exercised on a passing and a failing case, and
the adapter contract is run against deliberately broken adapters to prove
it catches each class of governance bypass.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vsl import Outcome
from vsl.testing import (
    EXPENSE_ACTION,
    AsyncSideEffect,
    SideEffect,
    assert_decision_evidence,
    assert_human_queued,
    assert_insufficient_evidence,
    assert_integrity_failed,
    assert_integrity_ok,
    assert_ledger_grew_by,
    assert_not_suspended,
    assert_proceeded,
    assert_suspended,
    assert_suspension_active,
    build_harness,
    entries_for_decision,
    latest_decision_id,
    ledger_entries,
    low_confidence_context,
    ok_context,
    over_cap_context,
    resolve_suspension,
    suspend,
)
from vsl.testing.contract import AdapterContract

from ledger_helpers import edit_line

ROOT = Path(__file__).resolve().parent.parent


def failing(call, *args, **kwargs) -> str:
    """Return the message of the AssertionError `call` must raise."""

    with pytest.raises(AssertionError) as caught:
        call(*args, **kwargs)

    return str(caught.value)


# =====================================================================
# SIDE EFFECTS
# =====================================================================


def test_side_effect_records_calls_and_returns_its_result():
    effect = SideEffect(result=7)

    assert not effect.executed
    assert effect("a", k=1) == 7
    assert effect.executed and effect.call_count == 1
    assert effect.calls == [(("a",), {"k": 1})]


def test_side_effect_that_raises_is_still_recorded_as_executed():
    effect = SideEffect(raises=RuntimeError("x"))

    with pytest.raises(RuntimeError):
        effect()

    effect.assert_executed(1)


def test_side_effect_accepts_an_exception_class():
    effect = SideEffect(raises=KeyError)

    with pytest.raises(KeyError):
        effect()


def test_side_effect_observes_before_acting():
    seen = []
    effect = SideEffect(observe=lambda: seen.append("observed") or len(seen))

    effect()

    assert effect.observations == [1]


def test_side_effect_assertions_fail_with_a_clear_message():
    effect = SideEffect(name="payment")

    assert "expected 1" in failing(effect.assert_executed, 1)

    effect()

    message = failing(effect.assert_not_executed, "denied")
    assert "payment executed 1 time(s)" in message and "denied" in message


@pytest.mark.asyncio
async def test_async_side_effect_is_awaitable_and_recorded():
    effect = AsyncSideEffect(result="r")

    assert await effect(1) == "r"
    effect.assert_executed(1)


@pytest.mark.asyncio
async def test_async_side_effect_failure_is_recorded():
    effect = AsyncSideEffect(raises=ValueError("no"))

    with pytest.raises(ValueError):
        await effect()

    effect.assert_executed(1)


# =====================================================================
# HARNESS
# =====================================================================


def test_harness_builds_a_real_governance_over_a_jsonl_ledger(tmp_path):
    harness = build_harness(tmp_path)

    assert harness.ledger_path == tmp_path / "ledger.jsonl"
    assert harness.policy_path.read_text(encoding="utf-8").startswith("schema_version")
    assert harness.entries() == []


def test_harness_memory_ledger_has_no_file(tmp_path):
    harness = build_harness(tmp_path, ledger="memory")

    assert harness.ledger_path is None
    asyncio.run(harness.authorize(EXPENSE_ACTION, ok_context()))
    assert len(harness.entries()) == 3


def test_harness_rejects_an_unknown_ledger_kind(tmp_path):
    with pytest.raises(ValueError, match="ledger must be"):
        build_harness(tmp_path, ledger="redis")


def test_harness_environment_is_hermetic_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPENSE_CAP_NZD", "1")  # must be ignored
    harness = build_harness(tmp_path)

    decision = asyncio.run(harness.authorize(EXPENSE_ACTION, ok_context()))

    assert_proceeded(decision)


def test_harness_side_effect_snapshots_the_ledger(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    asyncio.run(harness.run(EXPENSE_ACTION, ok_context(), effect))

    assert effect.observations == [["MONITOR", "PRE_NODE", "VERIFICATION"]]


@pytest.mark.asyncio
async def test_harness_entries_for_a_decision(tmp_path):
    harness = build_harness(tmp_path)
    first = await harness.authorize(EXPENSE_ACTION, ok_context())
    await harness.authorize(EXPENSE_ACTION, low_confidence_context())

    assert len(harness.entries_for(first)) == 3
    assert len(harness.entries()) == 6
    assert len(entries_for_decision(harness, first.decision_id)) == 3


# =====================================================================
# DECISION ASSERTIONS: pass on the right outcome, fail on the wrong one
# =====================================================================


@pytest.fixture
def decisions(tmp_path):
    """One decision per outcome, each from a fresh governed environment."""

    async def make():
        out = {}

        for name, context in [
            ("proceed", ok_context()),
            ("human", low_confidence_context()),
            ("suspended", over_cap_context()),
        ]:
            harness = build_harness(_mk(tmp_path, name))
            out[name] = (harness, await harness.authorize(EXPENSE_ACTION, context))

        return out

    return asyncio.run(make())


def _mk(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def test_decision_assertions_accept_the_right_outcome(decisions):
    assert_proceeded(decisions["proceed"][1])
    assert_human_queued(decisions["human"][1])
    assert_suspended(decisions["suspended"][1], terminal_state="expense-approvals-suspended")


def test_decision_assertions_reject_the_wrong_outcome(decisions):
    proceed, human, suspended = (decisions[k][1] for k in ("proceed", "human", "suspended"))

    assert "expected PROCEED" in failing(assert_proceeded, human)
    assert "expected PROCEED" in failing(assert_proceeded, suspended)
    assert "expected HUMAN_QUEUE" in failing(assert_human_queued, proceed)
    assert "expected SUSPENDED" in failing(assert_suspended, human)


def test_assert_suspended_checks_the_terminal_state(decisions):
    assert "terminal state" in failing(
        assert_suspended, decisions["suspended"][1], terminal_state="other"
    )


# =====================================================================
# EVIDENCE ASSERTIONS
# =====================================================================


def test_evidence_matches_each_outcome(decisions):
    for name in ("proceed", "human", "suspended"):
        harness, decision = decisions[name]
        entries = assert_decision_evidence(harness.governance, decision)

        assert entries == entries_for_decision(harness.governance, decision.decision_id)


def test_insufficient_evidence_requires_a_real_violation(decisions):
    harness, decision = decisions["suspended"]
    assert_insufficient_evidence(harness.governance, decision)

    harness, decision = decisions["human"]
    assert "expected SUSPENDED" in failing(assert_insufficient_evidence, harness.governance, decision)


def test_evidence_check_reports_a_wrong_outcome(decisions):
    """Evidence for a PROCEED must not satisfy a HUMAN_QUEUE claim."""

    harness, proceed = decisions["proceed"]
    claim = type("D", (), {"decision_id": proceed.decision_id, "outcome": Outcome.HUMAN_QUEUE, "terminal_state": None})()

    assert "denied PRE_NODE" in failing(assert_decision_evidence, harness.governance, claim)


def test_evidence_check_reports_missing_entries(tmp_path):
    harness = build_harness(tmp_path)
    ghost = type("D", (), {"decision_id": "nope", "outcome": Outcome.PROCEED, "terminal_state": None})()

    assert "no ledger entries" in failing(assert_decision_evidence, harness.governance, ghost)


def test_evidence_check_reports_a_broken_caused_by_link(tmp_path):
    harness = build_harness(tmp_path)
    decision = asyncio.run(harness.authorize(EXPENSE_ACTION, ok_context()))

    edit_line(harness.ledger_path, 1, caused_by="somewhere-else")

    assert "caused_by" in failing(assert_decision_evidence, harness.governance, decision)


def test_evidence_check_reports_a_wrong_verification_result(tmp_path):
    harness = build_harness(tmp_path)
    decision = asyncio.run(harness.authorize(EXPENSE_ACTION, ok_context()))

    harness_lines = harness.ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(harness_lines) == 3

    from vsl.testing import tamper_payload

    tamper_payload(harness.ledger_path, 2, result="INSUFFICIENT")

    assert "expected VERIFICATION SUFFICIENT" in failing(
        assert_decision_evidence, harness.governance, decision
    )


def test_latest_decision_id_and_empty_ledger(tmp_path):
    harness = build_harness(tmp_path)

    assert "empty" in failing(latest_decision_id, harness.governance)

    decision = asyncio.run(harness.authorize(EXPENSE_ACTION, ok_context()))

    assert latest_decision_id(harness) == decision.decision_id


# =====================================================================
# SUSPENSION / LIFECYCLE HELPERS
# =====================================================================


def test_suspension_assertions_and_lifecycle_helpers(tmp_path):
    harness = build_harness(tmp_path)

    assert "expected an active suspension" in failing(assert_suspension_active, harness.governance)
    assert_not_suspended(harness.governance)

    decision = asyncio.run(suspend(harness.governance))
    assert_suspended(decision)

    assert assert_suspension_active(harness.governance)
    assert "unexpected active suspension" in failing(assert_not_suspended, harness.governance)

    resolve_suspension(harness.governance)

    assert_not_suspended(harness.governance)
    assert_integrity_ok(harness.governance)


def test_suspend_helper_refuses_a_context_that_does_not_suspend(tmp_path):
    harness = build_harness(tmp_path)

    with pytest.raises(AssertionError, match="expected SUSPENDED"):
        asyncio.run(suspend(harness.governance, context=ok_context()))


def test_resolve_suspension_without_a_violation_is_an_error(tmp_path):
    harness = build_harness(tmp_path)

    with pytest.raises(AssertionError, match="no INSUFFICIENT"):
        resolve_suspension(harness.governance)


def test_integrity_assertions_both_ways(tmp_path):
    harness = build_harness(tmp_path)
    asyncio.run(harness.authorize(EXPENSE_ACTION, ok_context()))

    assert_integrity_ok(harness.governance)
    assert "expected integrity verification to fail" in failing(assert_integrity_failed, harness.governance)

    edit_line(harness.ledger_path, 0, entry_hash="f" * 64)

    assert_integrity_failed(harness.governance)
    assert "integrity verification failed" in failing(assert_integrity_ok, harness.governance)


def test_ledger_growth_assertion(tmp_path):
    harness = build_harness(tmp_path)
    before = len(ledger_entries(harness))

    asyncio.run(harness.authorize(EXPENSE_ACTION, ok_context()))

    assert_ledger_grew_by(harness.governance, before, 3)
    assert "grew by 3" in failing(assert_ledger_grew_by, harness.governance, before, 2)


# =====================================================================
# THE CONTRACT CATCHES BROKEN ADAPTERS
# =====================================================================


def run_contract_methods(adapter: AdapterContract, tmp_path_factory) -> dict[str, bool]:
    """Run each contract test on `adapter`; map test name -> passed."""

    results: dict[str, bool] = {}

    for name, method in inspect.getmembers(adapter, inspect.ismethod):
        if not name.startswith("test_"):
            continue

        params = inspect.signature(method).parameters
        kwargs = {}

        if "tmp_path" in params:
            kwargs["tmp_path"] = tmp_path_factory.mktemp(name)

        patch = pytest.MonkeyPatch()

        if "monkeypatch" in params:
            kwargs["monkeypatch"] = patch

        try:
            method(**kwargs)
            results[name] = True
        except BaseException:  # AssertionError and pytest's Failed both count
            results[name] = False
        finally:
            patch.undo()

    return results


class CorrectAdapter(AdapterContract):
    async def invoke(self, governance, action, context, effect):
        result = await governance.run(action, context, effect)
        return result.decision.outcome


class EffectBeforeGovernance(AdapterContract):
    """The classic bypass: do the consequential thing, then ask."""

    async def invoke(self, governance, action, context, effect):
        effect()
        decision = await governance.authorize(action, context)
        return decision.outcome


class IgnoresTheDecision(AdapterContract):
    """Authorises, then runs the effect whatever the answer was."""

    async def invoke(self, governance, action, context, effect):
        decision = await governance.authorize(action, context)
        effect()
        return decision.outcome


class SwallowsEffectErrors(AdapterContract):
    async def invoke(self, governance, action, context, effect):
        try:
            result = await governance.run(action, context, effect)
        except RuntimeError:
            return Outcome.PROCEED
        return result.decision.outcome


class TurnsGovernanceErrorsIntoDenials(AdapterContract):
    """Every governance failure becomes a polite HUMAN_QUEUE."""

    async def invoke(self, governance, action, context, effect):
        try:
            result = await governance.run(action, context, effect)
        except (ValueError, OSError):
            return Outcome.HUMAN_QUEUE
        return result.decision.outcome


class ApprovesDuringSuspension(AdapterContract):
    """Skips governance entirely once anything has been suspended."""

    async def invoke(self, governance, action, context, effect):
        if governance.active_suspension() is not None:
            effect()
            return Outcome.PROCEED

        result = await governance.run(action, context, effect)
        return result.decision.outcome


class MutatesTheContext(AdapterContract):
    async def invoke(self, governance, action, context, effect):
        context["touched"] = True
        result = await governance.run(action, context, effect)
        return result.decision.outcome


def test_the_contract_passes_a_correct_adapter(tmp_path_factory):
    results = run_contract_methods(CorrectAdapter(), tmp_path_factory)

    assert len(results) == 10
    assert all(results.values()), results


@pytest.mark.parametrize(
    "broken, must_fail",
    [
        (
            EffectBeforeGovernance,
            {
                "test_human_queue_never_executes_the_effect",
                "test_invariant_violation_suspends_and_blocks_the_effect",
                "test_unknown_action_raises_and_does_not_execute",
            },
        ),
        (
            IgnoresTheDecision,
            {
                "test_human_queue_never_executes_the_effect",
                "test_invariant_violation_suspends_and_blocks_the_effect",
                "test_suspension_blocks_every_later_action",
            },
        ),
        (SwallowsEffectErrors, {"test_effect_exception_propagates_and_is_not_a_denial"}),
        (
            TurnsGovernanceErrorsIntoDenials,
            {
                "test_unknown_action_raises_and_does_not_execute",
                "test_ledger_failure_stops_the_effect_and_propagates",
            },
        ),
        (
            ApprovesDuringSuspension,
            {
                "test_suspension_blocks_every_later_action",
                "test_reenable_requires_the_human_flow_and_then_resumes",
            },
        ),
        (MutatesTheContext, {"test_the_callers_context_is_not_mutated"}),
    ],
)
def test_the_contract_catches_each_kind_of_broken_adapter(broken, must_fail, tmp_path_factory):
    results = run_contract_methods(broken(), tmp_path_factory)
    failed = {name for name, passed in results.items() if not passed}

    assert must_fail <= failed, f"contract missed: {must_fail - failed}"


# =====================================================================
# PYTEST PLUGIN (opt-in)
# =====================================================================


def test_the_pytest_plugin_provides_working_fixtures(tmp_path):
    (tmp_path / "conftest.py").write_text(
        'pytest_plugins = ["vsl.testing.pytest_plugin"]\n', encoding="utf-8"
    )
    (tmp_path / "test_uses_fixtures.py").write_text(
        "import asyncio\n"
        "from vsl.testing import EXPENSE_ACTION, ok_context, assert_proceeded\n\n"
        "def test_jsonl(vsl_harness):\n"
        "    d = asyncio.run(vsl_harness.authorize(EXPENSE_ACTION, ok_context()))\n"
        "    assert_proceeded(d)\n"
        "    assert vsl_harness.ledger_path.exists()\n\n"
        "def test_memory(vsl_memory_harness):\n"
        "    d = asyncio.run(vsl_memory_harness.authorize(EXPENSE_ACTION, ok_context()))\n"
        "    assert_proceeded(d)\n"
        "    assert vsl_memory_harness.ledger_path is None\n",
        encoding="utf-8",
    )

    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_the_plugin_is_not_registered_automatically(tmp_path):
    (tmp_path / "test_no_plugin.py").write_text(
        "def test_x(vsl_harness):\n    pass\n", encoding="utf-8"
    )

    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "fixture 'vsl_harness' not found" in result.stdout
