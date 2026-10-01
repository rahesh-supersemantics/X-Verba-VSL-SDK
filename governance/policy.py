from __future__ import annotations

import os

from vsl_core.constructs import (
    Fallback,
    Invariant,
    PreNode,
    TerminalState,
)
from vsl_core.metrics import (
    AssuranceBasis,
    F2Modification,
    GammaEstimate,
    ROBUST_GAMMA_DEFAULT_THRESHOLD,
)


OUTPUT_LAYER = AssuranceBasis(
    f1_pre_commitment=True,
    f2_modification=F2Modification.NONE,
)


EXPENSE_SUSPENDED = TerminalState(
    name="expense-approvals-suspended",
    description=(
        "Automated expense approval halted. "
        "Human re-enablement required."
    ),
    entry_conditions=("expense-cap violated",),
)


DECISION_THRESHOLD = float(
    os.environ.get("DECISION_THRESHOLD", "0.50")
)

EXPENSE_CAP_NZD = float(
    os.environ.get("EXPENSE_CAP_NZD", "2000")
)


def confidence_gamma(
    p: float,
    calibration_error: float = 0.10,
) -> GammaEstimate:
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    t = min(max(DECISION_THRESHOLD, 1e-6), 1 - 1e-6)

    return GammaEstimate(
        gamma_hat=(
            (p / (1 - p))
            / (t / (1 - t))
        ),
        delta_estimation_error=calibration_error,
        energy_gap_estimate=p - t,
    )


async def _approval_confidence(
    context: dict,
) -> GammaEstimate:
    return confidence_gamma(
        context["confidence"]
    )


async def _within_cap(
    context: dict,
) -> bool:
    return (
        context["currency"] == "NZD"
        and float(context["amount"]) <= EXPENSE_CAP_NZD
    )


APPROVAL_CONFIDENCE = PreNode(
    name="approval-confidence",
    description=(
        "The model's policy-compliance confidence must "
        "clear the robust Gamma threshold before "
        "auto-approval. Denial routes to a human."
    ),
    monitor=_approval_confidence,
    assurance_basis=OUTPUT_LAYER,
    gamma_threshold=ROBUST_GAMMA_DEFAULT_THRESHOLD,
    fallback=Fallback(
        on_failure="ROUTE_TO_HUMAN_QUEUE",
        max_retries=0,
    ),
)


EXPENSE_CAP = Invariant(
    name="expense-cap",
    description=(
        "No expense above the NZD cap or in another "
        "currency is ever auto-approved."
    ),
    rule=_within_cap,
    assurance_basis=OUTPUT_LAYER,
    on_violation=EXPENSE_SUSPENDED,
)


ALL_PRE_NODES = (
    APPROVAL_CONFIDENCE,
)

ALL_INVARIANTS = (
    EXPENSE_CAP,
)

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionPolicy:
    pre_node: Any
    invariants: tuple[Any, ...]


@dataclass(frozen=True)
class Policy:
    agent: str
    actions: dict[str, ActionPolicy]


def build_policy() -> Policy:
    return Policy(
        agent="expense-agent",
        actions={
            "approve_expense": ActionPolicy(
                pre_node=APPROVAL_CONFIDENCE,
                invariants=(EXPENSE_CAP,),
            ),
        },
    )