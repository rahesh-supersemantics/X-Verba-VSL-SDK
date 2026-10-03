"""``governed_node``: authorise first, run the consequential effect only
if governance permits it."""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Mapping, Union

from vsl.governance import Governance
from vsl.integrations.common import resolve_context
from vsl.outcome import Outcome

from .errors import GovernedNodeError
from .record import DEFAULT_KEY, make_record

State = Any
ContextFn = Callable[[State], Union[Mapping[str, Any], Awaitable[Mapping[str, Any]]]]
Effect = Callable[[State], Union[Mapping[str, Any], None, Awaitable[Union[Mapping[str, Any], None]]]]


def governed_node(
    governance: Governance,
    action: str,
    *,
    context_fn: ContextFn,
    effect: Effect,
    key: str = DEFAULT_KEY,
) -> Callable[[State], Awaitable[dict[str, Any]]]:
    """Build an async LangGraph node that governs ``effect``.

    For each run the node:

    1. builds the governance context with ``context_fn(state)``,
    2. calls ``governance.authorize(action, context)``,
    3. calls ``effect(state)`` **only** if the outcome is PROCEED,
    4. returns the effect's state update (if any) plus a
       :class:`GovernanceRecord` under ``key``.

    ``effect`` is the consequential operation. It may be sync or async and
    returns a state update (a mapping) or ``None``. When governance does
    not permit execution it is never called and the update contains only
    the governance record.

    The node never mutates ``state``; it returns a new update.

    What propagates unchanged (never converted to a denial): exceptions
    from ``governance.authorize`` (unknown action, ledger failure,
    integrity failure) and exceptions from ``effect``. If governance
    itself fails, the effect has not run. Incorrect wiring raises
    :class:`GovernedNodeError`.

    This node is ``async`` because ``Governance.authorize`` is: run the
    graph with ``ainvoke``/``astream``. LangGraph refuses ``invoke`` on an
    async-only node before any code of this node runs.
    """

    if not callable(context_fn) or not callable(effect):
        raise GovernedNodeError("context_fn and effect must be callable")

    async def node(state: State) -> dict[str, Any]:
        context = await resolve_context(
            context_fn, state, action=action, error=GovernedNodeError
        )

        decision = await governance.authorize(action, context)

        # Fail closed: only an unambiguous PROCEED runs the effect.
        permitted = decision.outcome is Outcome.PROCEED and decision.allowed

        update: dict[str, Any] = {}

        if permitted:
            produced = effect(state)

            if inspect.isawaitable(produced):
                produced = await produced

            if produced is not None:
                if not isinstance(produced, Mapping):
                    raise GovernedNodeError(
                        f"The effect for action {action!r} must return a "
                        f"state-update mapping or None, got "
                        f"{type(produced).__name__}. (The effect ran; "
                        "the ledger holds the governance evidence.)"
                    )

                if key in produced:
                    raise GovernedNodeError(
                        f"The effect for action {action!r} returned the "
                        f"reserved governance key {key!r}. Use a different "
                        "key= for the node. (The effect ran; the ledger "
                        "holds the governance evidence.)"
                    )

                update.update(produced)

        update[key] = make_record(action, decision, performed=permitted)

        return update

    node.__name__ = f"governed_{action}"
    node.__qualname__ = node.__name__

    return node


def add_governed_node(
    builder: Any,
    name: str,
    governance: Governance,
    action: str,
    *,
    context_fn: ContextFn,
    effect: Effect,
    key: str = DEFAULT_KEY,
) -> Callable[[State], Awaitable[dict[str, Any]]]:
    """``builder.add_node(name, governed_node(...))`` with a build-time
    check that ``key`` is declared in the graph's state schema.

    LangGraph silently drops a returned key the schema does not declare.
    For a governed node that would mean the effect runs and its governance
    record is lost, so this refuses to build such a graph.
    """

    channels = getattr(builder, "channels", None)

    if channels is not None and key not in channels:
        raise GovernedNodeError(
            f"State key {key!r} is not declared in the graph's state "
            "schema. LangGraph would silently drop the governance record. "
            "Add the key to the state class."
        )

    node = governed_node(
        governance,
        action,
        context_fn=context_fn,
        effect=effect,
        key=key,
    )

    builder.add_node(name, node)

    return node
