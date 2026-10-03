"""The reusable adapter contract, run against ``Governance.run`` itself.

``Governance.run`` is the no-framework adapter. Passing the contract here
proves the contract is satisfiable and states the baseline every framework
adapter is held to.
"""

from __future__ import annotations

from vsl.testing.contract import AdapterContract


class TestGovernanceRunSatisfiesTheContract(AdapterContract):
    async def invoke(self, governance, action, context, effect):
        result = await governance.run(action, context, effect)

        # `performed` must agree with what the effect actually did.
        assert result.performed is effect.executed

        return result.decision.outcome
