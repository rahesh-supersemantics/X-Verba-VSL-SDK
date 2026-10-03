"""``GovernedToolMiddleware``: authorise a tool call before it runs.

Built on LangChain's own interception point for tool execution,
``AgentMiddleware.awrap_tool_call``, the same one Super Semantics'
``vsl-langchain`` adapter uses. The middleware receives the tool call
strictly before the tool executes and decides whether ``handler`` (the real
call) is ever invoked.

Unlike ``vsl-langchain``'s middleware, which evaluates pre-compiled gates
and writes nothing to the ledger, this calls ``Governance.authorize``, so
every governed tool call leaves MONITOR / PRE_NODE / VERIFICATION (and
TERMINAL) evidence linked by ``caused_by``. Gating itself stays in
VSL-Core, reached through the SDK; nothing is re-implemented here.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Union

try:
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.messages import ToolMessage
except ImportError as exc:  # pragma: no cover - exercised in a subprocess test
    raise ImportError(
        "vsl.integrations.langchain requires LangChain >= 1.0 "
        "(create_agent middleware). Install it with: "
        "pip install 'super-semantics-vsl-sdk[langchain]'"
    ) from exc

from vsl.governance import Governance
from vsl.integrations.common import (
    GovernedTool,
    authorize_tool_call,
    bind_tools,
    check_governance,
    is_permitted,
    refusal_text,
)


def _tool_arguments(request: Any) -> Mapping[str, Any]:
    return request.tool_call.get("args") or {}


class GovernedToolMiddleware(AgentMiddleware):
    """Governs named tools inside a ``create_agent()`` agent.

    ``tools`` maps a tool's name to the governance action that must be
    authorised before it runs: ``{"send_refund": "approve_refund"}``, or
    ``{"send_refund": GovernedTool("approve_refund", context_fn=...)}`` to
    control what governance evaluates (default: the tool's arguments).
    Tools not listed are not governed and pass straight through.

    For a governed call:

    * PROCEED: the real tool runs and its result is returned untouched.
    * HUMAN_QUEUE or SUSPENDED: the tool is **not** run. The model gets a
      ``ToolMessage`` with ``status="error"`` stating the outcome, and the
      message's ``artifact`` holds the :class:`GovernanceRecord`. Nothing
      retries or approves.
    * Governance failure (unknown action, ledger failure) or a wiring error
      raises, unchanged, out of the agent run; the tool is not run.

    Exceptions raised by the tool itself are LangChain's to handle and are
    not touched here.

    Governance is async, so this defines only ``awrap_tool_call``: run the
    agent with ``ainvoke``/``astream``. LangChain raises
    ``NotImplementedError`` for a synchronous run, before any tool executes.
    """

    def __init__(
        self,
        governance: Governance,
        tools: Mapping[str, Union[str, GovernedTool]],
    ) -> None:
        check_governance(governance, what="GovernedToolMiddleware")

        super().__init__()

        self._governance = governance
        self._tools = bind_tools(tools, what="GovernedToolMiddleware")

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        name = request.tool_call["name"]
        binding = self._tools.get(name)

        if binding is None:
            return await handler(request)

        record = await authorize_tool_call(
            self._governance,
            binding,
            request,
            default_context=_tool_arguments,
        )

        if not is_permitted(record):
            return ToolMessage(
                content=refusal_text(record),
                tool_call_id=request.tool_call["id"],
                name=name,
                status="error",
                artifact=record,
            )

        return await handler(request)
