from __future__ import annotations

from vsl.integrations.common import IntegrationError


class GovernedNodeError(IntegrationError):
    """The LangGraph integration was used incorrectly.

    Raised for problems in how a governed node is wired or what it is
    given: a context function that does not return a mapping, an effect
    that returns something LangGraph cannot use as a state update, a
    governance-state key that collides with the effect's output or is not
    declared in the graph's state schema, or a router handed a state with
    no valid governance record.

    It is never used for a governance decision (those are values, see
    :class:`vsl.Outcome`), for an exception raised by the application's own
    effect, or for a ledger failure; those propagate unchanged.
    """
