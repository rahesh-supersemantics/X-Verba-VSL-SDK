"""Microsoft Agent Framework integration for X-Verba governance.

    from vsl.integrations.maf import GovernedFunctionMiddleware, GovernedTool

Requires the ``agent-framework`` package:
``pip install "super-semantics-vsl-sdk[maf]"``. Importing this package
without it raises ``ImportError`` with that hint; the rest of the SDK is
unaffected.
"""

from vsl.integrations.common import GovernanceRecord, GovernedTool, IntegrationError

from .middleware import METADATA_KEY, GovernedFunctionMiddleware

__all__ = [
    "METADATA_KEY",
    "GovernanceRecord",
    "GovernedFunctionMiddleware",
    "GovernedTool",
    "IntegrationError",
]
