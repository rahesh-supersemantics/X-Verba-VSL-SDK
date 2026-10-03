"""
Phase 7 -- the LangChain adapter, tested with real ``create_agent`` agents.

The model is a deterministic fake chat model (no network); everything else
is real: LangChain's agent loop and ToolNode, the middleware, and a real
Governance with policy and ledger from the Phase 5 testing framework.

Skipped as a whole only if LangChain (>= 1.0) is not installed; it is an
optional extra, never a core dependency.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

pytest.importorskip("langchain.agents")

from langchain.agents import create_agent  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import StructuredTool, tool  # noqa: E402

from vsl import Outcome  # noqa: E402
from vsl.integrations.common import GovernedTool, IntegrationError  # noqa: E402
from vsl.integrations.langchain import GovernedToolMiddleware  # noqa: E402
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


class ScriptedModel(BaseChatModel):
    """Calls the given tool calls once, then reports what the tools returned."""

    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        results = [m for m in messages if isinstance(m, ToolMessage)]

        if results:
            text = " | ".join(f"{m.content} [{m.status}]" for m in results)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="final: " + text))])

        message = AIMessage(content="", tool_calls=list(self.calls))
        return ChatResult(generations=[ChatGeneration(message=message)])


def tool_call(name: str, args: dict | None = None, id: str = "c1") -> dict:
    return {"name": name, "args": args or {}, "id": id, "type": "tool_call"}


def make_tool(name: str, effect: Any) -> Any:
    def run() -> str:
        return str(effect())

    return StructuredTool.from_function(func=run, name=name, description=f"{name} tool")


class Spy:
    """Records every Decision Governance.authorize returns."""

    def __init__(self, governance: Any) -> None:
        self.decisions: list[Any] = []
        original = governance.authorize

        async def authorize(*args: Any, **kwargs: Any) -> Any:
            decision = await original(*args, **kwargs)
            self.decisions.append(decision)
            return decision

        governance.authorize = authorize


async def run_agent(governance: Any, tools: list, bindings: dict, calls: list[dict]) -> dict:
    agent = create_agent(
        model=ScriptedModel(calls=calls),
        tools=tools,
        middleware=[GovernedToolMiddleware(governance, bindings)],
    )

    return await agent.ainvoke({"messages": [HumanMessage("go")]})


# =====================================================================
# THE SHARED ADAPTER CONTRACT
# =====================================================================


class TestLangChainSatisfiesTheContract(AdapterContract):
    async def invoke(self, governance, action, context, effect):
        spy = Spy(governance)
        binding = GovernedTool(action, context_fn=lambda request: context)

        await run_agent(
            governance,
            [make_tool("pay", effect)],
            {"pay": binding},
            [tool_call("pay")],
        )

        assert len(spy.decisions) == 1
        return spy.decisions[0].outcome


# =====================================================================
# WHAT THE MODEL AND CALLER OBSERVE
# =====================================================================


@pytest.mark.asyncio
async def test_proceed_returns_the_real_tool_result(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="paid-150")

    final = await run_agent(
        harness.governance,
        [make_tool("pay", effect)],
        {"pay": GovernedTool(EXPENSE_ACTION, context_fn=lambda r: ok_context())},
        [tool_call("pay")],
    )

    message = final["messages"][-2]
    assert isinstance(message, ToolMessage)
    assert message.content == "paid-150" and message.status != "error"
    effect.assert_executed(1)


@pytest.mark.asyncio
async def test_human_queue_returns_an_error_tool_message_with_the_record(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    final = await run_agent(
        harness.governance,
        [make_tool("pay", effect)],
        {"pay": GovernedTool(EXPENSE_ACTION, context_fn=lambda r: low_confidence_context())},
        [tool_call("pay")],
    )

    message = final["messages"][-2]
    assert message.status == "error"
    assert message.name == "pay" and message.tool_call_id == "c1"
    assert "human" in message.content.lower()
    assert message.artifact["outcome"] == Outcome.HUMAN_QUEUE.value
    assert message.artifact["performed"] is False
    assert message.artifact["requires_human_review"] is True
    effect.assert_not_executed()

    # The agent loop continued: the model saw the denial and answered.
    assert final["messages"][-1].content.startswith("final:")


@pytest.mark.asyncio
async def test_suspended_returns_an_error_tool_message_and_stays_suspended(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect()
    bindings = {"pay": GovernedTool(EXPENSE_ACTION, context_fn=lambda r: over_cap_context())}

    final = await run_agent(harness.governance, [make_tool("pay", effect)], bindings, [tool_call("pay")])

    message = final["messages"][-2]
    assert message.status == "error"
    assert message.artifact["outcome"] == Outcome.SUSPENDED.value
    assert message.artifact["suspended"] is True
    effect.assert_not_executed()

    # No retry happened: exactly one decision's worth of evidence.
    assert [e.entry_type.value for e in ledger_entries(harness.governance)] == [
        "MONITOR", "PRE_NODE", "VERIFICATION", "TERMINAL",
    ]


# =====================================================================
# BINDING BEHAVIOUR
# =====================================================================


@pytest.mark.asyncio
async def test_unlisted_tools_pass_through_and_write_no_evidence(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="x")

    await run_agent(
        harness.governance,
        [make_tool("lookup", effect), make_tool("pay", harness.side_effect())],
        {"pay": EXPENSE_ACTION},
        [tool_call("lookup")],
    )

    effect.assert_executed(1)
    assert ledger_entries(harness.governance) == []


@pytest.mark.asyncio
async def test_default_context_is_the_tools_arguments(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="ok")

    @tool
    def pay(confidence: float, amount: float, currency: str) -> str:
        """Pay."""
        return str(effect())

    final = await run_agent(
        harness.governance,
        [pay],
        {"pay": EXPENSE_ACTION},
        [tool_call("pay", ok_context())],
    )

    assert final["messages"][-2].content == "ok"
    effect.assert_executed(1)

    monitor = ledger_entries(harness.governance)[0]
    assert monitor.entry_type.value == "MONITOR"
    assert "NZD" in str(monitor.payload)


@pytest.mark.asyncio
async def test_two_tool_calls_in_one_turn_are_each_governed(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="x")
    spy = Spy(harness.governance)

    contexts = iter([ok_context(), low_confidence_context()])
    binding = GovernedTool(EXPENSE_ACTION, context_fn=lambda r: next(contexts))

    final = await run_agent(
        harness.governance,
        [make_tool("pay", effect)],
        {"pay": binding},
        [tool_call("pay", id="c1"), tool_call("pay", id="c2")],
    )

    assert sorted(d.outcome.value for d in spy.decisions) == ["human_queue", "proceed"]
    effect.assert_executed(1)
    assert_integrity_ok(harness.governance)
    assert_causal_chain_valid(harness.governance)


@pytest.mark.asyncio
async def test_a_failing_context_fn_raises_and_the_tool_does_not_run(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    def bad(_: Any) -> dict:
        raise KeyError("no amount")

    with pytest.raises(KeyError):
        await run_agent(
            harness.governance,
            [make_tool("pay", effect)],
            {"pay": GovernedTool(EXPENSE_ACTION, context_fn=bad)},
            [tool_call("pay")],
        )

    effect.assert_not_executed()


@pytest.mark.asyncio
async def test_async_context_fn_is_supported(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect(result="ok")

    async def context(_: Any) -> dict:
        return ok_context()

    await run_agent(
        harness.governance,
        [make_tool("pay", effect)],
        {"pay": GovernedTool(EXPENSE_ACTION, context_fn=context)},
        [tool_call("pay")],
    )

    effect.assert_executed(1)


# =====================================================================
# WIRING AND LIMITS
# =====================================================================


def test_wiring_errors(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)

    with pytest.raises(IntegrationError):
        GovernedToolMiddleware(object(), {"pay": "a"})  # type: ignore[arg-type]

    with pytest.raises(IntegrationError):
        GovernedToolMiddleware(harness.governance, {})


def test_a_synchronous_run_is_refused_by_langchain_before_any_tool_runs(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    effect = harness.side_effect()
    agent = create_agent(
        model=ScriptedModel(calls=[tool_call("pay")]),
        tools=[make_tool("pay", effect)],
        middleware=[
            GovernedToolMiddleware(
                harness.governance,
                {"pay": GovernedTool(EXPENSE_ACTION, context_fn=lambda r: ok_context())},
            )
        ],
    )

    with pytest.raises(NotImplementedError):
        agent.invoke({"messages": [HumanMessage("go")]})

    effect.assert_not_executed("sync run with async-only middleware")


# =====================================================================
# MUTATION CHECK: the tests above fail if the gate is removed
# =====================================================================


@pytest.mark.asyncio
async def test_a_pass_through_middleware_would_be_caught(tmp_path: Any) -> None:
    """Proves the denial tests are not vacuous: with the gate disabled the
    same scenario runs the effect."""

    class Ungoverned(GovernedToolMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            return await handler(request)

    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    agent = create_agent(
        model=ScriptedModel(calls=[tool_call("pay")]),
        tools=[make_tool("pay", effect)],
        middleware=[Ungoverned(harness.governance, {"pay": EXPENSE_ACTION})],
    )
    await agent.ainvoke({"messages": [HumanMessage("go")]})

    assert effect.executed


# =====================================================================
# OPTIONAL DEPENDENCY
# =====================================================================


def test_importing_the_sdk_does_not_import_langchain() -> None:
    code = (
        "import sys, vsl, vsl.testing, vsl.integrations; "
        "assert 'langchain' not in sys.modules, 'langchain imported'"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
