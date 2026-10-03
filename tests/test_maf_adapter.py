"""
Phase 8 -- the Microsoft Agent Framework adapter, tested with real agents.

A real ``agent_framework.Agent`` runs its real function-invocation loop
against a deterministic fake chat client (no network, no Azure). Governance,
policy and ledger are real and come from the Phase 5 testing framework.

Skipped as a whole only if ``agent-framework`` is not installed; it is an
optional extra, never a core dependency.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

pytest.importorskip("agent_framework")

from agent_framework import (  # noqa: E402
    Agent,
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    FunctionMiddleware,
    Message,
    MiddlewareFailure,
    tool,
)

from vsl import Outcome  # noqa: E402
from vsl.integrations.common import GovernedTool, IntegrationError  # noqa: E402
from vsl.integrations.maf import METADATA_KEY, GovernedFunctionMiddleware  # noqa: E402
from vsl.testing import (  # noqa: E402
    EXPENSE_ACTION,
    assert_causal_chain_valid,
    assert_integrity_ok,
    build_harness,
    ledger_entries,
    low_confidence_context,
    ok_context,
    over_cap_context,
)
from vsl.testing.contract import AdapterContract  # noqa: E402


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    """Requests the given function calls once, then reports their results."""

    def __init__(self, calls: list[tuple[str, dict]]) -> None:
        super().__init__()
        self._calls = calls

    def _inner_get_response(self, *, messages, stream, options, **kwargs):  # type: ignore[override]
        async def respond() -> ChatResponse:
            results = [
                str(c.result)
                for m in messages
                for c in m.contents
                if c.type == "function_result"
            ]

            if results:
                return ChatResponse(
                    messages=[Message("assistant", [Content.from_text("final: " + " | ".join(results))])]
                )

            calls = [
                Content.from_function_call(call_id=f"c{i}", name=name, arguments=args)
                for i, (name, args) in enumerate(self._calls)
            ]
            return ChatResponse(messages=[Message("assistant", calls)])

        return respond()


def make_tool(name: str, effect: Any) -> Any:
    @tool(name=name, description=f"{name} tool", approval_mode="never_require")
    def run() -> str:
        return str(effect())

    return run


class Spy:
    def __init__(self, governance: Any) -> None:
        self.decisions: list[Any] = []
        original = governance.authorize

        async def authorize(*args: Any, **kwargs: Any) -> Any:
            decision = await original(*args, **kwargs)
            self.decisions.append(decision)
            return decision

        governance.authorize = authorize


class Observe(FunctionMiddleware):
    """Captures context.metadata after governance has run."""

    def __init__(self) -> None:
        self.metadata: list[dict] = []

    async def process(self, context: Any, call_next: Any) -> None:
        await call_next()
        self.metadata.append(dict(context.metadata))


async def run_agent(governance: Any, tools: list, bindings: dict, calls: list, *, extra: list | None = None):
    agent = Agent(
        client=ScriptedClient(calls),
        name="tester",
        instructions="x",
        tools=tools,
        middleware=[*(extra or []), GovernedFunctionMiddleware(governance, bindings)],
    )

    return await agent.run("go")


# =====================================================================
# THE SHARED ADAPTER CONTRACT
# =====================================================================


class TestMafSatisfiesTheContract(AdapterContract):
    # Declared properties of Microsoft Agent Framework (verified in the
    # tests below): it turns an ordinary tool exception into a tool-error
    # result, and governance failures surface as MiddlewareFailure.
    effect_errors_propagate = False
    governance_failure_wrapper = MiddlewareFailure

    async def invoke(self, governance, action, context, effect):
        spy = Spy(governance)
        binding = GovernedTool(action, context_fn=lambda ctx: context)

        await run_agent(governance, [make_tool("pay", effect)], {"pay": binding}, [("pay", {})])

        assert len(spy.decisions) == 1
        return spy.decisions[0].outcome


# =====================================================================
# WHAT THE MODEL AND CALLER OBSERVE
# =====================================================================


@pytest.mark.asyncio
async def test_proceed_runs_the_tool_and_records_the_decision(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="paid-150")
    observe = Observe()

    response = await run_agent(
        harness.governance,
        [make_tool("pay", effect)],
        {"pay": GovernedTool(EXPENSE_ACTION, context_fn=lambda c: ok_context())},
        [("pay", {})],
        extra=[observe],
    )

    assert "paid-150" in response.text
    effect.assert_executed(1)

    record = observe.metadata[0][METADATA_KEY]
    assert record["outcome"] == Outcome.PROCEED.value and record["performed"] is True


@pytest.mark.asyncio
async def test_human_queue_skips_the_tool_and_tells_the_model(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    response = await run_agent(
        harness.governance,
        [make_tool("pay", effect)],
        {"pay": GovernedTool(EXPENSE_ACTION, context_fn=lambda c: low_confidence_context())},
        [("pay", {})],
    )

    effect.assert_not_executed()
    assert response.text.startswith("final:")
    assert "human" in response.text.lower()


@pytest.mark.asyncio
async def test_suspended_skips_the_tool_and_records_terminal_evidence(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    response = await run_agent(
        harness.governance,
        [make_tool("pay", effect)],
        {"pay": GovernedTool(EXPENSE_ACTION, context_fn=lambda c: over_cap_context())},
        [("pay", {})],
    )

    effect.assert_not_executed()
    assert "suspend" in response.text.lower()
    assert [e.entry_type.value for e in ledger_entries(harness.governance)] == [
        "MONITOR", "PRE_NODE", "VERIFICATION", "TERMINAL",
    ]


# =====================================================================
# BINDING BEHAVIOUR
# =====================================================================


@pytest.mark.asyncio
async def test_unlisted_functions_pass_through_and_write_no_evidence(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="x")

    await run_agent(
        harness.governance,
        [make_tool("lookup", effect), make_tool("pay", harness.side_effect())],
        {"pay": EXPENSE_ACTION},
        [("lookup", {})],
    )

    effect.assert_executed(1)
    assert ledger_entries(harness.governance) == []


@pytest.mark.asyncio
async def test_default_context_is_the_functions_arguments(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="ok")

    @tool(name="pay", description="pay", approval_mode="never_require")
    def pay(confidence: float, amount: float, currency: str) -> str:
        return str(effect())

    response = await run_agent(
        harness.governance, [pay], {"pay": EXPENSE_ACTION}, [("pay", ok_context())]
    )

    assert "ok" in response.text
    effect.assert_executed(1)
    assert "NZD" in str(ledger_entries(harness.governance)[0].payload)


@pytest.mark.asyncio
async def test_two_calls_in_one_turn_are_each_governed(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="x")
    spy = Spy(harness.governance)

    contexts = iter([ok_context(), low_confidence_context()])
    binding = GovernedTool(EXPENSE_ACTION, context_fn=lambda c: next(contexts))

    await run_agent(
        harness.governance,
        [make_tool("pay", effect)],
        {"pay": binding},
        [("pay", {}), ("pay", {})],
    )

    assert sorted(d.outcome.value for d in spy.decisions) == ["human_queue", "proceed"]
    effect.assert_executed(1)
    assert_integrity_ok(harness.governance)
    assert_causal_chain_valid(harness.governance)


# =====================================================================
# FAIL CLOSED
# =====================================================================


@pytest.mark.asyncio
async def test_governance_failure_aborts_the_run_with_the_cause(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    with pytest.raises(MiddlewareFailure) as info:
        await run_agent(
            harness.governance,
            [make_tool("pay", effect)],
            {"pay": GovernedTool("not_an_action", context_fn=lambda c: ok_context())},
            [("pay", {})],
        )

    assert isinstance(info.value.__cause__, ValueError)
    effect.assert_not_executed()


@pytest.mark.asyncio
async def test_a_failing_context_fn_also_fails_closed(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    def bad(_: Any) -> dict:
        raise KeyError("no amount")

    with pytest.raises(MiddlewareFailure) as info:
        await run_agent(
            harness.governance,
            [make_tool("pay", effect)],
            {"pay": GovernedTool(EXPENSE_ACTION, context_fn=bad)},
            [("pay", {})],
        )

    assert isinstance(info.value.__cause__, KeyError)
    effect.assert_not_executed()


def test_wiring_errors(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)

    with pytest.raises(IntegrationError):
        GovernedFunctionMiddleware(object(), {"pay": "a"})  # type: ignore[arg-type]

    with pytest.raises(IntegrationError):
        GovernedFunctionMiddleware(harness.governance, {})


# =====================================================================
# MUTATION CHECK
# =====================================================================


@pytest.mark.asyncio
async def test_a_pass_through_middleware_would_be_caught(tmp_path: Any) -> None:
    class Ungoverned(GovernedFunctionMiddleware):
        async def process(self, context: Any, call_next: Any) -> None:
            await call_next()

    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    agent = Agent(
        client=ScriptedClient([("pay", {})]),
        name="t",
        instructions="x",
        tools=make_tool("pay", effect),
        middleware=[Ungoverned(harness.governance, {"pay": EXPENSE_ACTION})],
    )
    await agent.run("go")

    assert effect.executed


# =====================================================================
# OPTIONAL DEPENDENCY
# =====================================================================


def test_importing_the_sdk_does_not_import_agent_framework() -> None:
    code = (
        "import sys, vsl, vsl.testing, vsl.integrations; "
        "assert 'agent_framework' not in sys.modules, 'agent_framework imported'"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
