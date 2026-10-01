"""Named monitor and rule factories.

YAML cannot hold executable logic, so it refers to monitors and rules
by NAME. This registry maps those names to Python factories that turn
declarative `params` into the async callables VSL-Core's PreNode and
Invariant expect. The registry builds inputs for VSL-Core constructs;
it does not evaluate governance itself.

Registration happens in Python code the application controls. YAML can
never register, import or reference arbitrary callables.

There is no module-level global registry: default_registry() returns a
fresh Registry containing only the built-ins, so tests and applications
cannot affect each other.

Built-ins
---------
monitor ``confidence_gamma``
    The Spec's confidence -> Gamma mapping (SPEC 7.1), measured
    against the decision boundary's odds, never against 0.5.
rule ``max_value``
    ``context[field] <= max``.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Mapping

from vsl_core.metrics import GammaEstimate

from .exceptions import RegistryError

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_EPSILON = 1e-6


# =====================================================================
# STRICT VALUE HELPERS
# =====================================================================


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(
        value, bool
    )


def _require_param_number(name: str, value: Any) -> float:
    if not _is_number(value) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")

    return float(value)


def _require_param_field(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("field must be a non-empty string.")

    return value


def _context_number(
    context: Mapping[str, Any],
    field: str,
) -> float:
    """Read a numeric context value, failing loudly on anything else.

    A missing field raises KeyError and a non-numeric value (including
    strings and booleans) raises TypeError. Both propagate to the
    caller: no PROCEED, so no side effect.
    """

    value = context[field]

    if not _is_number(value):
        raise TypeError(
            f"context[{field!r}] must be a number, got "
            f"{type(value).__name__}."
        )

    return float(value)


# =====================================================================
# BUILT-IN: confidence_gamma
# =====================================================================


def confidence_gamma(
    *,
    decision_threshold: float,
    calibration_error: float = 0.10,
    field: str = "confidence",
) -> Callable[[Mapping[str, Any]], Any]:
    """Monitor factory: the Spec's confidence -> Gamma mapping.

        gamma_hat = (p / (1 - p)) / (t / (1 - t))

    where p is ``context[field]`` and t is ``decision_threshold``.
    `decision_threshold` is REQUIRED: dividing by 0.5 when the real
    decision boundary differs makes the PreNode deny every action.

    VSL-Core ships no estimator and this mapping is only as good as the
    calibration of the underlying probability.
    """

    t_value = _require_param_number(
        "decision_threshold",
        decision_threshold,
    )

    if not 0.0 < t_value < 1.0:
        raise ValueError(
            "decision_threshold must be strictly between 0 and 1."
        )

    delta = _require_param_number(
        "calibration_error",
        calibration_error,
    )

    if delta < 0.0:
        raise ValueError("calibration_error must be >= 0.")

    key = _require_param_field(field)

    t = min(max(t_value, _EPSILON), 1 - _EPSILON)

    async def monitor(context: Mapping[str, Any]) -> GammaEstimate:
        p = _context_number(context, key)

        p = min(max(p, _EPSILON), 1 - _EPSILON)

        return GammaEstimate(
            gamma_hat=(p / (1 - p)) / (t / (1 - t)),
            delta_estimation_error=delta,
            energy_gap_estimate=p - t,
        )

    return monitor


# =====================================================================
# BUILT-IN: max_value
# =====================================================================


def max_value(
    *,
    field: str,
    max: float,  # noqa: A002 - YAML parameter name
) -> Callable[[Mapping[str, Any]], Any]:
    """Rule factory: ``context[field] <= max``.

    Only the upper bound is checked. A NaN value fails the rule
    (comparison is False), so it is treated as a violation.
    """

    key = _require_param_field(field)

    limit = _require_param_number("max", max)

    async def rule(context: Mapping[str, Any]) -> bool:
        return _context_number(context, key) <= limit

    return rule


# =====================================================================
# REGISTRY
# =====================================================================


class Registry:
    """Maps names used in YAML to monitor and rule factories."""

    def __init__(self) -> None:
        self._monitors: dict[str, Callable[..., Any]] = {}
        self._rules: dict[str, Callable[..., Any]] = {}

    # -- registration ---------------------------------------------------

    @staticmethod
    def _check_name(name: str) -> None:
        if not isinstance(name, str) or not _NAME.match(name):
            raise RegistryError(
                f"Invalid registry name {name!r}: use letters, "
                "digits, '.', '_' or '-'."
            )

    def register_monitor(
        self,
        name: str,
        factory: Callable[..., Any],
    ) -> None:
        self._register(self._monitors, "monitor", name, factory)

    def register_rule(
        self,
        name: str,
        factory: Callable[..., Any],
    ) -> None:
        self._register(self._rules, "rule", name, factory)

    def _register(
        self,
        table: dict[str, Callable[..., Any]],
        kind: str,
        name: str,
        factory: Callable[..., Any],
    ) -> None:
        self._check_name(name)

        if not callable(factory):
            raise RegistryError(
                f"{kind} {name!r}: factory must be callable."
            )

        if name in table:
            raise RegistryError(
                f"{kind} {name!r} is already registered."
            )

        table[name] = factory

    def monitor(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form: ``@registry.monitor("name")``."""

        def decorate(factory: Callable[..., Any]) -> Callable[..., Any]:
            self.register_monitor(name, factory)
            return factory

        return decorate

    def rule(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form: ``@registry.rule("name")``."""

        def decorate(factory: Callable[..., Any]) -> Callable[..., Any]:
            self.register_rule(name, factory)
            return factory

        return decorate

    # -- lookup ---------------------------------------------------------

    def get_monitor(self, name: str) -> Callable[..., Any]:
        try:
            return self._monitors[name]
        except KeyError:
            raise RegistryError(
                f"Unknown monitor {name!r}. Registered: "
                f"{sorted(self._monitors)}."
            ) from None

    def get_rule(self, name: str) -> Callable[..., Any]:
        try:
            return self._rules[name]
        except KeyError:
            raise RegistryError(
                f"Unknown rule {name!r}. Registered: "
                f"{sorted(self._rules)}."
            ) from None

    @property
    def monitors(self) -> tuple[str, ...]:
        return tuple(sorted(self._monitors))

    @property
    def rules(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))


def default_registry() -> Registry:
    """A new Registry containing only the built-in monitors and rules."""

    registry = Registry()

    registry.register_monitor("confidence_gamma", confidence_gamma)
    registry.register_rule("max_value", max_value)

    return registry
