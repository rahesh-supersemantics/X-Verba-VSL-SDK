"""LangGraph integration for X-Verba governance.

A thin layer over :class:`vsl.Governance`. It owns no governance
semantics: gates, ledger evidence, suspension and human re-enablement all
stay in VSL-Core and the SDK.

    from vsl.integrations.langgraph import (
        governed_node, add_governed_node, governed_router,
    )

Install LangGraph itself with ``pip install "super-semantics-vsl-sdk[langgraph]"``.
This package does not import ``langgraph``; it returns plain async node
functions and routers that LangGraph accepts.
"""

from .errors import GovernedNodeError
from .node import add_governed_node, governed_node
from .record import (
    DEFAULT_KEY,
    GovernanceRecord,
    get_governance_record,
)
from .router import governed_router

__all__ = [
    "DEFAULT_KEY",
    "GovernanceRecord",
    "GovernedNodeError",
    "add_governed_node",
    "get_governance_record",
    "governed_node",
    "governed_router",
]
