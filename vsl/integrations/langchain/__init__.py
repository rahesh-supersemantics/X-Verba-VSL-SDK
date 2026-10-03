"""LangChain integration for X-Verba governance.

    from vsl.integrations.langchain import GovernedToolMiddleware, GovernedTool

Requires LangChain >= 1.0 (``create_agent`` and its middleware):
``pip install "super-semantics-vsl-sdk[langchain]"``. Importing this
package without LangChain installed raises ``ImportError`` with that hint;
the rest of the SDK is unaffected.
"""

from vsl.integrations.common import GovernanceRecord, GovernedTool, IntegrationError

from .middleware import GovernedToolMiddleware

__all__ = [
    "GovernanceRecord",
    "GovernedTool",
    "GovernedToolMiddleware",
    "IntegrationError",
]
