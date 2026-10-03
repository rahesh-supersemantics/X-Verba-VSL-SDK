"""A small reference policy and contexts for governance tests.

The policy is YAML, loaded through ``Governance.from_yaml``, so every test
built on it also exercises the real policy pipeline. It is the same policy
as ``examples/vsl.yaml`` minus the ``ledger:`` section (tests inject their
own ledger) and with the environment-driven values fixed.

Behaviour of the reference policy for action ``approve_expense``:

    confidence below the Gamma threshold   -> PreNode denial -> HUMAN_QUEUE
    amount above the cap (2000)            -> Invariant violation
                                              -> INSUFFICIENT + TERMINAL
                                              -> SUSPENDED
    otherwise                              -> PROCEED
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

EXPENSE_ACTION = "approve_expense"
EXPENSE_TERMINAL_STATE = "expense-approvals-suspended"
EXPENSE_CAP = 2000

EXPENSE_POLICY_YAML = """\
schema_version: 1

agent:
  name: expense-agent

policy:
  name: expense-policy
  version: "1.0.0"

assurance_bases:
  output_layer:
    f1_pre_commitment: true
    f2_modification: NONE

terminal_states:
  - name: expense-approvals-suspended
    description: Automated expense approval halted. Human re-enablement required.
    entry_conditions: ["expense-cap violated"]

prenodes:
  - name: approval-confidence
    description: Model confidence must clear the robust Gamma threshold.
    monitor: confidence_gamma
    params:
      decision_threshold: 0.50
      calibration_error: 0.10
    assurance_basis: output_layer
    gamma_threshold: 1.1
    fallback: human_review

invariants:
  - name: expense-cap
    description: No expense above the cap is ever auto-approved.
    rule: max_value
    params:
      field: amount
      max: 2000
    assurance_basis: output_layer
    on_violation: expense-approvals-suspended

actions:
  approve_expense:
    prenodes: [approval-confidence]
    invariants: [expense-cap]
"""


def ok_context(
    *, confidence: float = 0.95, amount: float = 150
) -> dict[str, Any]:
    """Context the reference policy lets through (PROCEED)."""

    return {"confidence": confidence, "amount": amount, "currency": "NZD"}


def low_confidence_context() -> dict[str, Any]:
    """Context that triggers a PreNode denial (HUMAN_QUEUE)."""

    return ok_context(confidence=0.51)


def over_cap_context() -> dict[str, Any]:
    """Context that violates the invariant (INSUFFICIENT, SUSPENDED)."""

    return ok_context(amount=EXPENSE_CAP * 2.5)


def write_policy(
    directory: str | Path,
    text: str = EXPENSE_POLICY_YAML,
    *,
    name: str = "vsl.yaml",
) -> Path:
    """Write policy text to ``directory/name`` and return the path."""

    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path
