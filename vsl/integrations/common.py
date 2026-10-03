"""Pieces shared by every framework integration.

Nothing here imports a framework. Framework subpackages build on these so
that the decision record, the refusal text, tool bindings and the
"authorise this tool call" step exist exactly once.

Governance semantics stay in VSL-Core and :class:`vsl.Governance`; this
module only shapes their results for framework code.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, TypedDict, Union

from vsl.decision import Decision
from vsl.exceptions import SDKError
from vsl.governance import Governance
from vsl.outcome import Outcome


class IntegrationError(SDKError):
    """A framework integration was configured or used incorrectly.

    Never raised for a governance decision (those are values, see
    :class:`vsl.Outcome`), for an exception raised by the application's own
    operation, or for a ledger failure; those propagate unchanged.
    """


# ---------------------------------------------------------------------
# DECISION RECORD
# ---------------------------------------------------------------------


class GovernanceRecord(TypedDict):
    """A plain, serialisable snapshot of one governance decision.

    A dictionary rather than a ``Decision`` object so it survives
    framework checkpointing and serialisation unchanged. The ledger remains
    the evidence; ``decision_id`` is the join key to it.
    """

    action: str
    outcome: str  # an ``Outcome`` value: "proceed" | "human_queue" | "suspended"
    decision_id: str
    allowed: bool
    requires_human_review: bool
    suspended: bool
    performed: bool  # was the consequential operation run (or handed on to run)?
    reason: Optional[str]
    terminal_state: Optional[str]


def make_record(action: str, decision: Decision, *, performed: bool) -> GovernanceRecord:
    return GovernanceRecord(
        action=action,
        outcome=decision.outcome.value,
        decision_id=decision.decision_id,
        allowed=decision.allowed,
        requires_human_review=decision.requires_human_review,
        suspended=decision.suspended,
        performed=performed,
        reason=decision.reason,
        terminal_state=decision.terminal_state,
    )


def is_permitted(record: Mapping[str, Any]) -> bool:
    """Fail closed: only an unambiguous PROCEED permits execution."""

    return record.get("outcome") == Outcome.PROCEED.value and bool(record.get("allowed"))


def refusal_text(record: Mapping[str, Any]) -> str:
    """What a framework tells the model/caller when an action was not run.

    States the outcome; never suggests retrying or approving.
    """

    parts = [
        f"Not executed: governance outcome {str(record['outcome']).upper()} "
        f"for action '{record['action']}'."
    ]

    if record.get("requires_human_review"):
        parts.append("Human review is required.")

    if record.get("suspended"):
        terminal = record.get("terminal_state")
        parts.append(
            "Automation is suspended"
            + (f" ({terminal})" if terminal else "")
            + " until a human re-enables it."
        )

    if record.get("reason"):
        parts.append(f"Reason: {record['reason']}")

    parts.append(f"Decision {record['decision_id']}.")

    return " ".join(parts)


# ---------------------------------------------------------------------
# CONTEXT
# ---------------------------------------------------------------------

ContextFn = Callable[[Any], Union[Mapping[str, Any], Awaitable[Mapping[str, Any]]]]


async def resolve_context(
    context_fn: ContextFn,
    source: Any,
    *,
    action: str,
    error: type[IntegrationError] = IntegrationError,
) -> dict[str, Any]:
    """Run ``context_fn(source)`` (sync or async) and validate the result.

    Exceptions raised by ``context_fn`` itself propagate unchanged: invalid
    state is the caller's information, not a governance decision.
    """

    context = context_fn(source)

    if inspect.isawaitable(context):
        context = await context

    if not isinstance(context, Mapping):
        raise error(
            f"context_fn for action {action!r} must return a mapping, "
            f"got {type(context).__name__}"
        )

    return dict(context)


# ---------------------------------------------------------------------
# TOOL BINDINGS (LangChain, Microsoft Agent Framework)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class GovernedTool:
    """Binds a framework tool to a governance action.

    ``action``      the policy action to authorise before the tool runs.
    ``context_fn``  maps the framework's tool-call object to the
                    governance context (a mapping). Default: the tool's
                    arguments. Whatever it returns is written into the
                    ledger's MONITOR entry, so do not include secrets.
    """

    action: str
    context_fn: Optional[ContextFn] = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or not self.action:
            raise IntegrationError("GovernedTool.action must be a non-empty string")

        if self.context_fn is not None and not callable(self.context_fn):
            raise IntegrationError("GovernedTool.context_fn must be callable")


def bind_tools(
    tools: Mapping[str, Union[str, GovernedTool]],
    *,
    what: str,
) -> dict[str, GovernedTool]:
    """Validate and normalise ``{tool name: action | GovernedTool}``.

    A bare string is shorthand for ``GovernedTool(action)``.
    """

    if not isinstance(tools, Mapping) or not tools:
        raise IntegrationError(
            f"{what} needs a non-empty mapping of tool name to action"
        )

    bound: dict[str, GovernedTool] = {}

    for name, binding in tools.items():
        if not isinstance(name, str) or not name:
            raise IntegrationError(f"{what}: tool names must be non-empty strings")

        if isinstance(binding, str):
            binding = GovernedTool(binding)

        if not isinstance(binding, GovernedTool):
            raise IntegrationError(
                f"{what}: tool {name!r} must map to an action name or a "
                f"GovernedTool, got {type(binding).__name__}"
            )

        bound[name] = binding

    return bound


def check_governance(governance: Any, *, what: str) -> None:
    if not isinstance(governance, Governance):
        raise IntegrationError(
            f"{what} needs a vsl.Governance, got {type(governance).__name__}"
        )


async def authorize_tool_call(
    governance: Governance,
    binding: GovernedTool,
    source: Any,
    *,
    default_context: Callable[[Any], Mapping[str, Any]],
) -> GovernanceRecord:
    """Authorise one tool call and return the record of the decision.

    Calls ``Governance.authorize`` exactly once. Exceptions from it (unknown
    action, ledger failure) propagate unchanged; the caller decides how its
    framework surfaces them, but must not run the tool.
    """

    context = await resolve_context(
        binding.context_fn or default_context,
        source,
        action=binding.action,
    )

    decision = await governance.authorize(binding.action, context)

    return make_record(
        binding.action,
        decision,
        performed=decision.outcome is Outcome.PROCEED and decision.allowed,
    )
