"""Shared integration helpers (vsl.integrations.common): framework-free."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vsl import Outcome, SDKError
from vsl.integrations.common import (
    GovernedTool,
    IntegrationError,
    authorize_tool_call,
    bind_tools,
    check_governance,
    is_permitted,
    refusal_text,
    resolve_context,
)
from vsl.testing import (
    EXPENSE_ACTION,
    build_harness,
    ledger_entries,
    low_confidence_context,
    ok_context,
    over_cap_context,
)


def authorize(harness: Any, context: dict, binding: Any = None):
    binding = binding or GovernedTool(EXPENSE_ACTION)
    return asyncio.run(
        authorize_tool_call(
            harness.governance, binding, context, default_context=lambda c: c
        )
    )


def test_integration_error_is_an_sdk_error() -> None:
    assert issubclass(IntegrationError, SDKError)


def test_proceed_record_is_permitted(tmp_path: Any) -> None:
    record = authorize(build_harness(tmp_path), ok_context())

    assert record["outcome"] == Outcome.PROCEED.value
    assert record["performed"] is True
    assert is_permitted(record)


def test_human_queue_record_is_not_permitted(tmp_path: Any) -> None:
    record = authorize(build_harness(tmp_path), low_confidence_context())

    assert record["outcome"] == Outcome.HUMAN_QUEUE.value
    assert record["performed"] is False
    assert record["requires_human_review"] is True
    assert not is_permitted(record)
    assert "human" in refusal_text(record).lower()


def test_suspended_record_is_not_permitted_and_names_the_decision(tmp_path: Any) -> None:
    record = authorize(build_harness(tmp_path), over_cap_context())

    assert record["outcome"] == Outcome.SUSPENDED.value
    assert record["suspended"] is True
    assert not is_permitted(record)
    assert record["decision_id"] in refusal_text(record)


def test_authorize_calls_governance_exactly_once(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)
    authorize(harness, ok_context())

    monitors = [e for e in ledger_entries(harness.governance) if e.entry_type.value == "MONITOR"]
    assert len(monitors) == 1


def test_unknown_action_raises_and_writes_nothing(tmp_path: Any) -> None:
    harness = build_harness(tmp_path)

    with pytest.raises(ValueError, match="Unknown action"):
        authorize(harness, ok_context(), GovernedTool("nope"))

    assert ledger_entries(harness.governance) == []


def test_resolve_context_accepts_sync_and_async_and_copies() -> None:
    source = {"a": 1}

    async def run() -> None:
        assert await resolve_context(lambda s: s, source, action="x") == source

        async def async_fn(s: Any) -> dict:
            return {"b": 2}

        assert await resolve_context(async_fn, source, action="x") == {"b": 2}
        assert (await resolve_context(lambda s: s, source, action="x")) is not source

    asyncio.run(run())


def test_resolve_context_rejects_a_non_mapping() -> None:
    with pytest.raises(IntegrationError, match="must return a mapping"):
        asyncio.run(resolve_context(lambda s: [1], None, action="x"))


def test_resolve_context_does_not_swallow_the_callers_error() -> None:
    def boom(_: Any) -> dict:
        raise KeyError("missing")

    with pytest.raises(KeyError):
        asyncio.run(resolve_context(boom, None, action="x"))


def test_bind_tools_normalises_strings() -> None:
    bound = bind_tools({"pay": "approve"}, what="T")

    assert bound == {"pay": GovernedTool("approve")}


@pytest.mark.parametrize(
    "bad",
    [{}, None, {"": "a"}, {"t": 5}, {"t": ""}, {5: "a"}],
)
def test_bind_tools_rejects_bad_input(bad: Any) -> None:
    with pytest.raises(IntegrationError):
        bind_tools(bad, what="T")


def test_governed_tool_validates() -> None:
    with pytest.raises(IntegrationError):
        GovernedTool("")

    with pytest.raises(IntegrationError):
        GovernedTool("a", context_fn="nope")  # type: ignore[arg-type]


def test_check_governance_requires_a_governance() -> None:
    with pytest.raises(IntegrationError, match="vsl.Governance"):
        check_governance(object(), what="T")
