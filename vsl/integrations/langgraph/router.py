"""``governed_router``: route a conditional edge on the recorded outcome."""

from __future__ import annotations

from typing import Any, Callable

from vsl.outcome import Outcome

from .errors import GovernedNodeError
from .record import DEFAULT_KEY, get_governance_record


def governed_router(
    *,
    proceed: str,
    human_queue: str,
    suspended: str,
    key: str = DEFAULT_KEY,
) -> Callable[[Any], str]:
    """Build a router for ``add_conditional_edges``.

    Each argument is the name of the node (or ``END``) to go to for that
    outcome. All three are required: there is deliberately no default
    route, so no outcome can fall through to the wrong place.

    The router only reads the governance record a governed node already
    wrote under ``key``. It does not call governance and does not retry or
    approve anything. A state with no valid record raises
    :class:`GovernedNodeError` instead of being routed.
    """

    targets = {
        Outcome.PROCEED: proceed,
        Outcome.HUMAN_QUEUE: human_queue,
        Outcome.SUSPENDED: suspended,
    }

    for outcome, target in targets.items():
        if not isinstance(target, str) or not target:
            raise GovernedNodeError(
                f"governed_router needs a node name for {outcome.value}"
            )

    def route(state: Any) -> str:
        record = get_governance_record(state, key)

        return targets[Outcome(record["outcome"])]

    return route
