"""Compile a validated policy document into real VSL-Core constructs.

    validated YAML data
        -> AssuranceBasis / TerminalState / PreNode / Invariant
           (the actual vsl_core classes)
        -> CompiledPolicy (what Governance consumes)

No governance semantics live here. The result is the same shape the
programmatic `build_policy()` produces: `.agent` and `.actions`, where
each action has `.pre_node` and `.invariants`. Governance then compiles
the gates with VSL-Core's adapter exactly as before.

Assurance levels are never set: only the F1/F2 facts are passed to
AssuranceBasis and VSL-Core derives the level.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from vsl_core.constructs import (
    Fallback,
    Invariant,
    PreNode,
    TerminalState,
)
from vsl_core.metrics import (
    ROBUST_GAMMA_DEFAULT_THRESHOLD,
    AssuranceBasis,
    F2Modification,
)

from .config import PolicyConfig, load_policy_config
from .exceptions import PolicyValidationError, RegistryError
from .ledger import LedgerConfig
from .registry import Registry, default_registry


# =====================================================================
# COMPILED POLICY
# =====================================================================


@dataclass(frozen=True)
class CompiledAction:
    """The gates guarding one governed action."""

    pre_node: PreNode | None
    invariants: tuple[Invariant, ...]


@dataclass(frozen=True)
class CompiledPolicy:
    """A policy compiled to real VSL-Core constructs.

    `agent`, `actions` are what Governance reads. The remaining fields
    expose the real construct objects and the policy's identity.
    """

    name: str
    version: str
    agent: str
    policy_hash: str
    actions: Mapping[str, CompiledAction]
    assurance_bases: Mapping[str, AssuranceBasis]
    terminal_states: Mapping[str, TerminalState]
    pre_nodes: Mapping[str, PreNode]
    invariants: Mapping[str, Invariant]
    ledger: LedgerConfig | None = None

    def pre_node(self, name: str) -> PreNode:
        try:
            return self.pre_nodes[name]
        except KeyError:
            raise KeyError(f"Unknown prenode {name!r}.") from None

    def invariant(self, name: str) -> Invariant:
        try:
            return self.invariants[name]
        except KeyError:
            raise KeyError(f"Unknown invariant {name!r}.") from None

    def terminal_state(self, name: str) -> TerminalState:
        try:
            return self.terminal_states[name]
        except KeyError:
            raise KeyError(
                f"Unknown terminal state {name!r}."
            ) from None


# =====================================================================
# COMPILATION
# =====================================================================


def _build(
    factory: Any,
    params: Mapping[str, Any],
    where: str,
) -> Any:
    """Call a monitor/rule factory with YAML params.

    Wrong, missing or unexpected params and values rejected by the
    factory become PolicyValidationError naming the YAML location.
    """

    try:
        inspect.signature(factory).bind(**params)

    except TypeError as exc:
        raise PolicyValidationError(
            [f"{where}.params: {exc}"]
        ) from None

    try:
        return factory(**params)

    except (TypeError, ValueError) as exc:
        raise PolicyValidationError(
            [f"{where}.params: {exc}"]
        ) from None


def compile_policy(
    config: PolicyConfig,
    registry: Registry | None = None,
) -> CompiledPolicy:
    """Compile a validated PolicyConfig into a CompiledPolicy."""

    reg = registry if registry is not None else default_registry()
    data = config.data

    bases = {
        name: AssuranceBasis(
            f1_pre_commitment=basis["f1_pre_commitment"],
            f2_modification=F2Modification[basis["f2_modification"]],
        )
        for name, basis in data["assurance_bases"].items()
    }

    terminals = {
        t["name"]: TerminalState(
            name=t["name"],
            description=t["description"],
            entry_conditions=tuple(t.get("entry_conditions", ())),
        )
        for t in data["terminal_states"]
    }

    pre_nodes: dict[str, PreNode] = {}

    for index, spec in enumerate(data.get("prenodes", [])):
        monitor = _build(
            reg.get_monitor(spec["monitor"]),
            spec.get("params", {}),
            f"prenodes[{index}]",
        )

        pre_nodes[spec["name"]] = PreNode(
            name=spec["name"],
            description=spec["description"],
            monitor=monitor,
            assurance_basis=bases[spec["assurance_basis"]],
            gamma_threshold=spec.get(
                "gamma_threshold",
                ROBUST_GAMMA_DEFAULT_THRESHOLD,
            ),
            # The only supported fallback, "human_review", maps to the
            # Spec's human-queue fallback.
            fallback=Fallback(
                on_failure="ROUTE_TO_HUMAN_QUEUE",
                max_retries=0,
            ),
        )

    invariants: dict[str, Invariant] = {}

    for index, spec in enumerate(data.get("invariants", [])):
        rule = _build(
            reg.get_rule(spec["rule"]),
            spec.get("params", {}),
            f"invariants[{index}]",
        )

        invariants[spec["name"]] = Invariant(
            name=spec["name"],
            description=spec["description"],
            rule=rule,
            assurance_basis=bases[spec["assurance_basis"]],
            on_violation=terminals[spec["on_violation"]],
        )

    actions: dict[str, CompiledAction] = {}

    for action_name, spec in data["actions"].items():
        gate_prenodes = spec.get("prenodes", [])

        actions[action_name] = CompiledAction(
            pre_node=(
                pre_nodes[gate_prenodes[0]]
                if gate_prenodes
                else None
            ),
            invariants=tuple(
                invariants[name]
                for name in spec.get("invariants", [])
            ),
        )

    ledger_data = data.get("ledger")

    ledger = (
        LedgerConfig(
            backend=ledger_data["backend"],
            path=ledger_data.get("path"),
            fsync=ledger_data.get("fsync", True),
        )
        if ledger_data is not None
        else None
    )

    agent = data["agent"]

    return CompiledPolicy(
        name=data["policy"]["name"],
        version=data["policy"]["version"],
        agent=agent.get("identity_key", agent["name"]),
        policy_hash=config.policy_hash,
        actions=MappingProxyType(actions),
        assurance_bases=MappingProxyType(bases),
        terminal_states=MappingProxyType(terminals),
        pre_nodes=MappingProxyType(pre_nodes),
        invariants=MappingProxyType(invariants),
        ledger=ledger,
    )


def load_policy(
    path: Any,
    *,
    registry: Registry | None = None,
    env: Mapping[str, str] | None = None,
) -> CompiledPolicy:
    """Load, validate and compile a YAML policy file."""

    return compile_policy(
        load_policy_config(path, env=env),
        registry,
    )


# =====================================================================
# POLICY REGISTRY
# =====================================================================


class PolicyRegistry:
    """Holds compiled policies, keyed by (name, version).

    - registering the same (name, version) twice is an error
    - a name may have several versions
    - get(name) without a version succeeds only when exactly one
      version is registered; with several it raises rather than guess,
      because the SDK defines no version ordering
    - nothing is global: each registry is its own instance
    """

    def __init__(self) -> None:
        self._policies: dict[str, dict[str, CompiledPolicy]] = {}

    def register(self, policy: CompiledPolicy) -> CompiledPolicy:
        versions = self._policies.setdefault(policy.name, {})

        if policy.version in versions:
            raise RegistryError(
                f"Policy {policy.name!r} version "
                f"{policy.version!r} is already registered."
            )

        versions[policy.version] = policy

        return policy

    def load_yaml(
        self,
        path: Any,
        *,
        registry: Registry | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CompiledPolicy:
        """Load, validate, compile and register a YAML policy."""

        return self.register(
            load_policy(path, registry=registry, env=env)
        )

    def get(
        self,
        name: str,
        version: str | None = None,
    ) -> CompiledPolicy:
        versions = self._policies.get(name)

        if not versions:
            raise RegistryError(f"Unknown policy {name!r}.")

        if version is not None:
            try:
                return versions[version]
            except KeyError:
                raise RegistryError(
                    f"Policy {name!r} has no version {version!r}. "
                    f"Registered: {sorted(versions)}."
                ) from None

        if len(versions) > 1:
            raise RegistryError(
                f"Policy {name!r} has several versions "
                f"{sorted(versions)}; specify one."
            )

        return next(iter(versions.values()))

    def versions(self, name: str) -> tuple[str, ...]:
        if name not in self._policies:
            raise RegistryError(f"Unknown policy {name!r}.")

        return tuple(sorted(self._policies[name]))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._policies))

    def __contains__(self, name: object) -> bool:
        return name in self._policies

    def __len__(self) -> int:
        return sum(len(v) for v in self._policies.values())
