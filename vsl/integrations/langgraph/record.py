"""The governance record a governed node writes into graph state.

``GovernanceRecord`` and ``make_record`` live in
:mod:`vsl.integrations.common` (shared with the other integrations) and are
re-exported here unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping

from vsl.integrations.common import GovernanceRecord, make_record
from vsl.outcome import Outcome

from .errors import GovernedNodeError

DEFAULT_KEY = "governance"

__all__ = ["DEFAULT_KEY", "GovernanceRecord", "get_governance_record", "make_record"]


def get_governance_record(state: Any, key: str = DEFAULT_KEY) -> GovernanceRecord:
    """Read and validate the record stored under ``key`` in graph state.

    Works for dict-like state and for attribute-style state (for example a
    Pydantic model). Raises :class:`GovernedNodeError` if the record is
    missing or malformed, never returns a guess.
    """

    if isinstance(state, Mapping):
        record = state.get(key)
    else:
        record = getattr(state, key, None)

    if not isinstance(record, Mapping):
        raise GovernedNodeError(
            f"No governance record under state key {key!r}. Route on a "
            "governed node's record only after that node has run, and "
            "declare the key in the graph's state schema."
        )

    try:
        Outcome(record["outcome"])
    except (KeyError, ValueError):
        raise GovernedNodeError(
            f"The governance record under {key!r} has no valid 'outcome'."
        ) from None

    return record  # type: ignore[return-value]
