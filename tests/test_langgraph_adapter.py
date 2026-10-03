"""
Phase 6 -- the LangGraph adapter, tested with real compiled graphs.

Everything here runs actual LangGraph graphs against a real Governance,
policy and ledger built with the Phase 5 testing framework. The shared
adapter contract is run first; the rest covers what is specific to
LangGraph: state handling, routing, sync/async, checkpointing and wiring
errors.

Skipped as a whole only if LangGraph is not installed (it is an optional
extra, never a core dependency).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, TypedDict

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from vsl import Outcome, SDKError  # noqa: E402
from vsl.integrations.langgraph import (  # noqa: E402
    GovernanceRecord,
    GovernedNodeError,
    add_governed_node,
    get_governance_record,
    governed_node,
    governed_router,
)
from vsl.testing import (  # noqa: E402
    EXPENSE_ACTION,
    EXPENSE_POLICY_YAML,
    EXPENSE_TERMINAL_STATE,
    assert_causal_chain_valid,
    assert_decision_evidence,
    assert_integrity_ok,
    build_harness,
    ledger_entries,
    low_confidence_context,
    ok_context,
    over_cap_context,
    replace_in_policy,
    resolve_suspension,
    suspend,
)
from vsl.testing.contract import AdapterContract  # noqa: E402
from vsl.testing.side_effect import AsyncSideEffect, SideEffect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# =====================================================================
# A small, realistic expense graph
# =====================================================================


class ExpenseState(TypedDict, total=False):
    expense: dict
    note: str
    status: str
    governance: GovernanceRecord


def build_expense_app(
    governance: Any,
    effect: Any,
    *,
    action: str = EXPENSE_ACTION,
    checkpointer: Any = None,
):
    """START -> approve (governed) -> router -> done | human_queue | suspended."""

    def approve_effect(state: ExpenseState) -> dict:
        effect(state)
        return {"status": "approved"}

    graph = StateGraph(ExpenseState)

    add_governed_node(
        graph,
        "approve",
        governance,
        action,
        context_fn=lambda s: s["expense"],
        effect=approve_effect,
    )
    graph.add_node("human_queue", lambda s: {"status": "human_review"})
    graph.add_node("suspended", lambda s: {"status": "suspended"})

    graph.add_edge(START, "approve")
    graph.add_conditional_edges(
        "approve",
        governed_router(
            proceed=END, human_queue="human_queue", suspended="suspended"
        ),
    )
    graph.add_edge("human_queue", END)
    graph.add_edge("suspended", END)

    return graph.compile(checkpointer=checkpointer)


async def run_expense(governance: Any, context: dict, effect: Any, **state: Any):
    app = build_expense_app(governance, effect)
    return await app.ainvoke({"expense": context, **state})


# =====================================================================
# THE SHARED ADAPTER CONTRACT
# =====================================================================


class TestLangGraphSatisfiesTheContract(AdapterContract):
    async def invoke(self, governance, action, context, effect):
        app = build_expense_app(governance, effect, action=action)
        final = await app.ainvoke({"expense": context})

        record = final["governance"]

        # The record must agree with what the effect actually did.
        assert record["performed"] is effect.executed

        return Outcome(record["outcome"])


# =====================================================================
# OUTCOMES, THROUGH A REAL GRAPH
# =====================================================================


@pytest.mark.asyncio
async def test_proceed_runs_the_effect_and_records_the_decision(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    final = await run_expense(harness.governance, ok_context(), effect)

    effect.assert_executed(1)
    assert final["status"] == "approved"

    record = final["governance"]
    assert record["outcome"] == "proceed"
    assert record["allowed"] and record["performed"]
    assert not record["requires_human_review"] and not record["suspended"]

    # The record is the join key to ledger evidence.
    assert_decision_evidence(
        harness.governance,
        type("D", (), {"decision_id": record["decision_id"], "outcome": Outcome.PROCEED, "terminal_state": None})(),
    )


@pytest.mark.asyncio
async def test_human_queue_blocks_the_effect_and_exposes_review_state(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    final = await run_expense(harness.governance, low_confidence_context(), effect)

    effect.assert_not_executed()
    assert final["status"] == "human_review"  # routed, not approved

    record = final["governance"]
    assert record["outcome"] == "human_queue"
    assert record["requires_human_review"] is True
    assert record["allowed"] is False and record["performed"] is False
    assert record["suspended"] is False

    # No automatic approval and no suspension happened.
    types = [e.entry_type.value for e in ledger_entries(harness.governance)]
    assert "TERMINAL" not in types


@pytest.mark.asyncio
async def test_invariant_violation_is_suspended_with_insufficient_evidence(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    final = await run_expense(harness.governance, over_cap_context(), effect)

    effect.assert_not_executed()
    assert final["status"] == "suspended"

    record = final["governance"]

    # INSUFFICIENT is ledger evidence, not an Outcome the adapter invents.
    assert record["outcome"] == "suspended"
    assert record["suspended"] is True and record["performed"] is False
    assert record["terminal_state"] == EXPENSE_TERMINAL_STATE

    entries = [
        e for e in ledger_entries(harness.governance)
        if e.decision_id == record["decision_id"]
    ]
    assert [e.entry_type.value for e in entries] == [
        "MONITOR", "PRE_NODE", "VERIFICATION", "TERMINAL",
    ]
    assert entries[2].payload["result"] == "INSUFFICIENT"
    assert entries[3].caused_by == entries[2].entry_id


@pytest.mark.asyncio
async def test_prenode_denial_is_not_insufficient(tmp_path):
    harness = build_harness(tmp_path)

    final = await run_expense(harness.governance, low_confidence_context(), harness.side_effect())

    verification = [
        e for e in ledger_entries(harness.governance)
        if e.entry_type.value == "VERIFICATION"
    ]
    assert [v.payload["result"] for v in verification] == ["SUFFICIENT"]
    assert final["governance"]["outcome"] == "human_queue"


@pytest.mark.asyncio
async def test_a_second_run_while_suspended_is_blocked_by_the_suspension(tmp_path):
    harness = build_harness(tmp_path)
    await run_expense(harness.governance, over_cap_context(), harness.side_effect())

    effect = harness.side_effect()
    final = await run_expense(harness.governance, ok_context(), effect)  # would PROCEED

    effect.assert_not_executed()
    record = final["governance"]
    assert record["outcome"] == "suspended" and record["performed"] is False

    resolve_suspension(harness.governance)

    resumed = harness.side_effect()
    final = await run_expense(harness.governance, ok_context(), resumed)

    resumed.assert_executed(1)
    assert final["governance"]["outcome"] == "proceed"


# =====================================================================
# SIDE-EFFECT PROTECTION: effect and evidence ordering
# =====================================================================


@pytest.mark.asyncio
async def test_governance_evidence_exists_before_the_effect_runs(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    await run_expense(harness.governance, ok_context(), effect)

    assert effect.observations == [["MONITOR", "PRE_NODE", "VERIFICATION"]]


@pytest.mark.asyncio
async def test_an_async_effect_is_awaited(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.async_side_effect()

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: s["expense"],
        effect=lambda s: effect(s),  # returns a coroutine
    )
    graph.add_edge(START, "approve")
    graph.add_edge("approve", END)

    final = await graph.compile().ainvoke({"expense": ok_context()})

    effect.assert_executed(1)
    assert final["governance"]["performed"] is True


@pytest.mark.asyncio
async def test_an_async_effect_is_not_started_when_denied(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.async_side_effect()

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: s["expense"],
        effect=lambda s: effect(s),
    )
    graph.add_edge(START, "approve")
    graph.add_edge("approve", END)

    await graph.compile().ainvoke({"expense": over_cap_context()})

    effect.assert_not_executed()


@pytest.mark.asyncio
async def test_an_async_context_function_is_awaited(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    async def context_fn(state):
        return state["expense"]

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=context_fn, effect=lambda s: effect(s),
    )
    graph.add_edge(START, "approve")
    graph.add_edge("approve", END)

    await graph.compile().ainvoke({"expense": ok_context()})

    effect.assert_executed(1)


# =====================================================================
# SYNC EXECUTION
# =====================================================================


def test_sync_invoke_is_refused_before_anything_runs(tmp_path):
    """The SDK's governance is async, so governed nodes are async-only.
    LangGraph refuses `invoke` on them; nothing executes and nothing is
    written -- it fails closed rather than faking a sync path."""

    harness = build_harness(tmp_path)
    effect = harness.side_effect()
    app = build_expense_app(harness.governance, effect)

    with pytest.raises(TypeError, match="synchronous"):
        app.invoke({"expense": ok_context()})

    effect.assert_not_executed()
    assert ledger_entries(harness.governance) == []


# =====================================================================
# STATE PRESERVATION
# =====================================================================


@pytest.mark.asyncio
async def test_existing_state_is_preserved_and_the_input_is_not_mutated(tmp_path):
    harness = build_harness(tmp_path)
    initial = {"expense": ok_context(), "note": "keep me"}
    before = json.loads(json.dumps(initial))

    app = build_expense_app(harness.governance, harness.side_effect())
    final = await app.ainvoke(initial)

    assert initial == before                       # caller's input untouched
    assert final["note"] == "keep me"              # unrelated state survives
    assert final["expense"] == before["expense"]
    assert set(final["governance"]) == {
        "action", "outcome", "decision_id", "allowed",
        "requires_human_review", "suspended", "performed",
        "reason", "terminal_state",
    }


@pytest.mark.asyncio
async def test_the_record_is_plain_json_and_survives_a_checkpointer(tmp_path):
    harness = build_harness(tmp_path)
    app = build_expense_app(
        harness.governance, harness.side_effect(), checkpointer=MemorySaver()
    )
    config = {"configurable": {"thread_id": "t1"}}

    final = await app.ainvoke({"expense": over_cap_context()}, config)

    json.dumps(final["governance"])                # plain data

    restored = (await app.aget_state(config)).values["governance"]
    assert restored == final["governance"]
    assert restored["terminal_state"] == EXPENSE_TERMINAL_STATE


@pytest.mark.asyncio
async def test_a_rerun_of_the_same_graph_records_a_fresh_decision(tmp_path):
    harness = build_harness(tmp_path)
    app = build_expense_app(harness.governance, harness.side_effect())

    first = await app.ainvoke({"expense": ok_context()})
    second = await app.ainvoke({"expense": ok_context()})

    assert first["governance"]["decision_id"] != second["governance"]["decision_id"]
    assert len(ledger_entries(harness.governance)) == 6


@pytest.mark.asyncio
async def test_pydantic_state_is_supported(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    class PState(BaseModel):
        expense: dict
        status: Optional[str] = None
        governance: Optional[dict] = None

    graph = StateGraph(PState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: s.expense,
        effect=lambda s: (effect(s), {"status": "approved"})[1],
    )
    graph.add_node("held", lambda s: {"status": "held"})
    graph.add_edge(START, "approve")
    graph.add_conditional_edges(
        "approve",
        governed_router(proceed=END, human_queue="held", suspended="held"),
    )
    graph.add_edge("held", END)
    app = graph.compile()

    ok = await app.ainvoke({"expense": ok_context()})
    held = await app.ainvoke({"expense": over_cap_context()})

    assert ok["status"] == "approved" and ok["governance"]["performed"]
    assert held["status"] == "held" and held["governance"]["outcome"] == "suspended"
    effect.assert_executed(1)


# =====================================================================
# MULTIPLE GOVERNED NODES AND NON-GOVERNED NODES
# =====================================================================

TWO_ACTIONS_YAML = replace_in_policy(
    EXPENSE_POLICY_YAML,
    "    invariants: [expense-cap]\n",
    "    invariants: [expense-cap]\n"
    "  reimburse_expense:\n"
    "    prenodes: [approval-confidence]\n"
    "    invariants: [expense-cap]\n",
)


class ChainState(TypedDict, total=False):
    expense: dict
    fee: float
    log: list
    status: str
    approve_gov: GovernanceRecord
    reimburse_gov: GovernanceRecord


def build_chain(governance, approve_effect, reimburse_effect):
    """approve (governed) -> router -> reimburse (governed) -> router -> notify."""

    graph = StateGraph(ChainState)

    add_governed_node(
        graph, "approve", governance, "approve_expense",
        context_fn=lambda s: s["expense"],
        effect=lambda s: (approve_effect(s), {"log": ["approved"]})[1],
        key="approve_gov",
    )
    add_governed_node(
        graph, "reimburse", governance, "reimburse_expense",
        # The fee is added only for the second action.
        context_fn=lambda s: {
            **s["expense"],
            "amount": s["expense"]["amount"] + s.get("fee", 0),
        },
        effect=lambda s: (
            reimburse_effect(s),
            {"log": s["log"] + ["reimbursed"]},
        )[1],
        key="reimburse_gov",
    )
    graph.add_node("stop", lambda s: {"status": "stopped"})
    graph.add_node("notify", lambda s: {"status": "notified"})  # not governed

    graph.add_edge(START, "approve")
    graph.add_conditional_edges(
        "approve",
        governed_router(
            proceed="reimburse", human_queue="stop", suspended="stop",
            key="approve_gov",
        ),
    )
    graph.add_conditional_edges(
        "reimburse",
        governed_router(
            proceed="notify", human_queue="stop", suspended="stop",
            key="reimburse_gov",
        ),
    )
    graph.add_edge("stop", END)
    graph.add_edge("notify", END)

    return graph.compile()


@pytest.mark.asyncio
async def test_two_governed_nodes_each_keep_their_own_decision(tmp_path):
    harness = build_harness(tmp_path, policy_text=TWO_ACTIONS_YAML)
    first, second = harness.side_effect(name="approve"), harness.side_effect(name="reimburse")

    final = await build_chain(harness.governance, first, second).ainvoke(
        {"expense": ok_context(), "fee": 10}
    )

    first.assert_executed(1)
    second.assert_executed(1)
    assert final["status"] == "notified"
    assert final["log"] == ["approved", "reimbursed"]
    assert final["approve_gov"]["action"] == "approve_expense"
    assert final["reimburse_gov"]["action"] == "reimburse_expense"
    assert final["approve_gov"]["decision_id"] != final["reimburse_gov"]["decision_id"]

    assert len(ledger_entries(harness.governance)) == 6
    assert_integrity_ok(harness.governance)
    assert_causal_chain_valid(harness.governance)


@pytest.mark.asyncio
async def test_the_second_node_can_be_blocked_after_the_first_ran(tmp_path):
    harness = build_harness(tmp_path, policy_text=TWO_ACTIONS_YAML)
    first, second = harness.side_effect(name="approve"), harness.side_effect(name="reimburse")

    # 1990 clears the cap on its own; the fee pushes the second action over it.
    final = await build_chain(harness.governance, first, second).ainvoke(
        {"expense": ok_context(amount=1990), "fee": 100}
    )

    first.assert_executed(1)                       # legitimately permitted
    second.assert_not_executed("invariant violation")
    assert final["status"] == "stopped"
    assert final["approve_gov"]["outcome"] == "proceed"
    assert final["reimburse_gov"]["outcome"] == "suspended"
    assert final["reimburse_gov"]["performed"] is False


@pytest.mark.asyncio
async def test_a_blocked_first_node_means_the_second_never_runs(tmp_path):
    harness = build_harness(tmp_path, policy_text=TWO_ACTIONS_YAML)
    first, second = harness.side_effect(), harness.side_effect()

    final = await build_chain(harness.governance, first, second).ainvoke(
        {"expense": low_confidence_context()}
    )

    first.assert_not_executed()
    second.assert_not_executed()
    assert "reimburse_gov" not in final            # that node never ran
    assert final["approve_gov"]["outcome"] == "human_queue"
    assert len(ledger_entries(harness.governance)) == 3


@pytest.mark.asyncio
async def test_non_governed_nodes_are_unaffected_and_leave_no_ledger_trace(tmp_path):
    harness = build_harness(tmp_path)

    class PlainState(TypedDict, total=False):
        n: int

    graph = StateGraph(PlainState)
    graph.add_node("double", lambda s: {"n": s["n"] * 2})

    async def add_one(s):
        return {"n": s["n"] + 1}

    graph.add_node("add_one", add_one)
    graph.add_edge(START, "double")
    graph.add_edge("double", "add_one")
    graph.add_edge("add_one", END)

    assert await graph.compile().ainvoke({"n": 5}) == {"n": 11}
    assert ledger_entries(harness.governance) == []


# =====================================================================
# ERRORS: governance decision vs application error vs infrastructure
# =====================================================================


@pytest.mark.asyncio
async def test_a_governance_denial_is_a_value_not_an_exception(tmp_path):
    harness = build_harness(tmp_path)

    # Neither denial raises out of the graph.
    await run_expense(harness.governance, low_confidence_context(), harness.side_effect())
    await run_expense(harness.governance, over_cap_context(), harness.side_effect())


@pytest.mark.asyncio
async def test_an_effect_exception_propagates_unchanged(tmp_path):
    harness = build_harness(tmp_path)
    boom = RuntimeError("payment gateway down")
    effect = harness.side_effect(raises=boom)

    with pytest.raises(RuntimeError) as caught:
        await run_expense(harness.governance, ok_context(), effect)

    assert caught.value is boom
    assert not isinstance(caught.value, SDKError)  # not relabelled
    effect.assert_executed(1)


@pytest.mark.asyncio
async def test_an_unknown_action_raises_and_is_not_a_denial(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()
    app = build_expense_app(harness.governance, effect, action="nope")

    with pytest.raises(ValueError, match="Unknown action"):
        await app.ainvoke({"expense": ok_context()})

    effect.assert_not_executed()
    assert ledger_entries(harness.governance) == []


@pytest.mark.asyncio
async def test_a_ledger_failure_propagates_and_the_effect_does_not_run(tmp_path, monkeypatch):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    def down(*_a, **_k):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(harness.governance.ledger, "write_monitor", down)

    with pytest.raises(OSError, match="ledger unavailable"):
        await run_expense(harness.governance, ok_context(), effect)

    effect.assert_not_executed("ledger failure")


@pytest.mark.asyncio
async def test_a_failure_part_way_through_governance_does_not_run_the_effect(tmp_path, monkeypatch):
    """The ledger fails *after* MONITOR and PRE_NODE were written: still no
    effect, and the partial evidence is not hidden."""

    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    def down(*_a, **_k):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(harness.governance.ledger, "write_verification", down)

    with pytest.raises(OSError):
        await run_expense(harness.governance, ok_context(), effect)

    effect.assert_not_executed()
    assert [e.entry_type.value for e in ledger_entries(harness.governance)] == [
        "MONITOR", "PRE_NODE",
    ]


@pytest.mark.asyncio
async def test_a_context_function_returning_a_non_mapping_is_a_wiring_error(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: "not a mapping", effect=lambda s: effect(s),
    )
    graph.add_edge(START, "approve")
    graph.add_edge("approve", END)

    with pytest.raises(GovernedNodeError, match="mapping"):
        await graph.compile().ainvoke({"expense": ok_context()})

    effect.assert_not_executed()
    assert ledger_entries(harness.governance) == []


@pytest.mark.asyncio
async def test_a_context_function_that_raises_stops_before_governance(tmp_path):
    harness = build_harness(tmp_path)
    effect = harness.side_effect()

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: s["expense"]["missing"], effect=lambda s: effect(s),
    )
    graph.add_edge(START, "approve")
    graph.add_edge("approve", END)

    with pytest.raises(KeyError):                   # invalid state, surfaced as-is
        await graph.compile().ainvoke({"expense": ok_context()})

    effect.assert_not_executed()
    assert ledger_entries(harness.governance) == []


@pytest.mark.parametrize("bad", ["text", 5, ["a"]])
@pytest.mark.asyncio
async def test_an_effect_returning_a_non_mapping_is_a_wiring_error(tmp_path, bad):
    harness = build_harness(tmp_path)

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: s["expense"], effect=lambda s: bad,
    )
    graph.add_edge(START, "approve")
    graph.add_edge("approve", END)

    with pytest.raises(GovernedNodeError, match="state-update mapping"):
        await graph.compile().ainvoke({"expense": ok_context()})


@pytest.mark.asyncio
async def test_an_effect_that_returns_none_is_fine(tmp_path):
    harness = build_harness(tmp_path)

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: s["expense"], effect=lambda s: None,
    )
    graph.add_edge(START, "approve")
    graph.add_edge("approve", END)

    final = await graph.compile().ainvoke({"expense": ok_context()})

    assert final["governance"]["performed"] is True


@pytest.mark.asyncio
async def test_an_effect_returning_the_governance_key_is_rejected(tmp_path):
    harness = build_harness(tmp_path)

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: s["expense"],
        effect=lambda s: {"governance": "mine"},
    )
    graph.add_edge(START, "approve")
    graph.add_edge("approve", END)

    with pytest.raises(GovernedNodeError, match="reserved governance key"):
        await graph.compile().ainvoke({"expense": ok_context()})


# =====================================================================
# WIRING GUARDS
# =====================================================================


def test_add_governed_node_rejects_an_undeclared_state_key(tmp_path):
    """LangGraph silently drops undeclared keys, which would lose the
    governance record after the effect ran. The helper refuses to build."""

    harness = build_harness(tmp_path)

    class NoGovernance(TypedDict, total=False):
        expense: dict

    graph = StateGraph(NoGovernance)

    with pytest.raises(GovernedNodeError, match="not declared"):
        add_governed_node(
            graph, "approve", harness.governance, EXPENSE_ACTION,
            context_fn=lambda s: s["expense"], effect=lambda s: None,
        )

    assert "approve" not in graph.nodes


def test_governed_node_requires_callables(tmp_path):
    harness = build_harness(tmp_path)

    with pytest.raises(GovernedNodeError):
        governed_node(harness.governance, EXPENSE_ACTION, context_fn=None, effect=lambda s: None)

    with pytest.raises(GovernedNodeError):
        governed_node(harness.governance, EXPENSE_ACTION, context_fn=lambda s: {}, effect="no")


def test_router_without_a_record_fails_closed():
    route = governed_router(proceed="a", human_queue="b", suspended="c")

    with pytest.raises(GovernedNodeError, match="No governance record"):
        route({"expense": {}})


@pytest.mark.parametrize("record", [{}, {"outcome": "approved"}, {"outcome": None}, "proceed"])
def test_router_rejects_a_malformed_record(record):
    route = governed_router(proceed="a", human_queue="b", suspended="c")

    with pytest.raises(GovernedNodeError):
        route({"governance": record})


def test_router_maps_every_outcome_to_its_own_target():
    route = governed_router(proceed="go", human_queue="review", suspended="halt")

    for outcome, target in [("proceed", "go"), ("human_queue", "review"), ("suspended", "halt")]:
        assert route({"governance": {"outcome": outcome}}) == target


def test_router_requires_all_three_targets():
    with pytest.raises(GovernedNodeError):
        governed_router(proceed="a", human_queue="", suspended="c")

    with pytest.raises(TypeError):
        governed_router(proceed="a", human_queue="b")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_router_reading_the_wrong_key_raises_instead_of_misrouting(tmp_path):
    harness = build_harness(tmp_path)

    graph = StateGraph(ExpenseState)
    add_governed_node(
        graph, "approve", harness.governance, EXPENSE_ACTION,
        context_fn=lambda s: s["expense"], effect=lambda s: None,
    )
    graph.add_edge(START, "approve")
    graph.add_conditional_edges(
        "approve",
        governed_router(proceed=END, human_queue=END, suspended=END, key="other"),
    )

    with pytest.raises(GovernedNodeError):
        await graph.compile().ainvoke({"expense": ok_context()})


def test_get_governance_record_reads_attribute_style_state():
    class Holder:
        governance = {"outcome": "proceed"}

    assert get_governance_record(Holder())["outcome"] == "proceed"


# =====================================================================
# SUSPENSION STATE IS THE SDK'S, NOT THE ADAPTER'S
# =====================================================================


@pytest.mark.asyncio
async def test_suspension_created_outside_the_graph_blocks_the_graph(tmp_path):
    import asyncio

    harness = build_harness(tmp_path)
    await suspend(harness.governance)               # via Governance directly
    effect = harness.side_effect()

    final = await run_expense(harness.governance, ok_context(), effect)

    effect.assert_not_executed()
    assert final["governance"]["outcome"] == "suspended"
    assert final["governance"]["terminal_state"] == EXPENSE_TERMINAL_STATE


# =====================================================================
# PACKAGING: the adapter must not drag LangGraph into the core SDK
# =====================================================================


def _python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, capture_output=True, text=True,
    )


def test_importing_the_core_sdk_does_not_import_langgraph():
    result = _python(
        "import sys, vsl; import vsl.testing; "
        "assert 'langgraph' not in sys.modules, 'core SDK imported langgraph'"
    )
    assert result.returncode == 0, result.stderr


def test_the_adapter_itself_does_not_import_langgraph():
    result = _python(
        "import sys, vsl.integrations.langgraph as lg; "
        "assert 'langgraph' not in sys.modules, 'adapter imported langgraph'; "
        "assert lg.governed_node"
    )
    assert result.returncode == 0, result.stderr
