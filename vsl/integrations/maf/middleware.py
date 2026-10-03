"""``GovernedFunctionMiddleware``: authorise a function (tool) call before
it runs, as a Microsoft Agent Framework ``FunctionMiddleware``.

Built on the framework's documented function-middleware contract
(``agent-framework`` 1.19.0): ``process(context, call_next)`` runs before
each tool call and decides whether ``call_next()`` (the tool) is reached.

Two properties of that contract shape the design:

* An *ordinary* exception from function middleware is converted by the
  framework into a tool-error result and the loop keeps running. That is
  fail-open for an enforcement layer, so a failure to *obtain* a governance
  decision is raised as ``MiddlewareFailure``, the framework's fail-closed
  escape, which aborts the run and propagates to the caller.
* A denial is not an error: it is a decision. The tool is skipped and the
  model is told the outcome as the tool's result.

The existing ``vsl-maf`` package raises ``AutomationDeniedException`` from
``process()``; under current MAF that would be absorbed as a tool error and
write no ledger evidence. This middleware reaches governance through
``Governance.authorize`` so every governed call is recorded.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Union

try:
    from agent_framework import FunctionMiddleware, MiddlewareFailure
except ImportError as exc:  # pragma: no cover - exercised in a subprocess test
    raise ImportError(
        "vsl.integrations.maf requires the Microsoft Agent Framework "
        "(package 'agent-framework'). Install it with: "
        "pip install 'super-semantics-vsl-sdk[maf]'"
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

METADATA_KEY = "vsl_governance"


def _arguments(context: Any) -> Mapping[str, Any]:
    arguments = context.arguments

    if isinstance(arguments, Mapping):
        return arguments

    return arguments.model_dump()  # a Pydantic model, per the framework's contract


class GovernedFunctionMiddleware(FunctionMiddleware):
    """Governs named functions (tools) of a Microsoft Agent Framework agent.

    ``functions`` maps a function's name to the governance action that must
    be authorised before it runs: ``{"pay": "approve_payment"}``, or
    ``{"pay": GovernedTool("approve_payment", context_fn=...)}`` to control
    what governance evaluates (default: the function's arguments). Functions
    not listed are not governed and pass straight through.

    Pass it in the agent's middleware list::

        Agent(client=..., tools=pay, middleware=[GovernedFunctionMiddleware(gov, {...})])

    For a governed call, the :class:`GovernanceRecord` is stored in
    ``context.metadata["vsl_governance"]`` and then:

    * PROCEED: ``call_next()`` runs the tool.
    * HUMAN_QUEUE or SUSPENDED: the tool is **not** run; ``context.result``
      is set to a text stating the outcome, which the model receives as the
      tool's result. Nothing retries or approves.
    * Failure to obtain a decision (unknown action, ledger failure, a bad
      ``context_fn``): the tool is not run and ``MiddlewareFailure`` is
      raised, with the original exception as ``__cause__``.

    Exceptions from the tool itself (inside ``call_next()``) are left to the
    framework, which reports them to the model as a tool error.
    """

    def __init__(
        self,
        governance: Governance,
        functions: Mapping[str, Union[str, GovernedTool]],
    ) -> None:
        check_governance(governance, what="GovernedFunctionMiddleware")

        self._governance = governance
        self._functions = bind_tools(functions, what="GovernedFunctionMiddleware")

    async def process(
        self,
        context: Any,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        binding = self._functions.get(context.function.name)

        if binding is None:
            await call_next()
            return

        try:
            record = await authorize_tool_call(
                self._governance,
                binding,
                context,
                default_context=_arguments,
            )
        except Exception as exc:
            raise MiddlewareFailure(
                f"Governance could not decide on function "
                f"{context.function.name!r} (action {binding.action!r}); "
                f"it was not run. {type(exc).__name__}: {exc}"
            ) from exc

        context.metadata[METADATA_KEY] = record

        if not is_permitted(record):
            context.result = refusal_text(record)
            return

        await call_next()
