"""Policy file loading, interpolation and validation.

Pipeline (each stage fails closed with a distinct SDK exception):

    read file            -> PolicyConfigError
    safe YAML parse      -> PolicyConfigError
    forbidden keys       -> PolicyConfigError
    env interpolation    -> PolicyConfigError
    JSON Schema          -> PolicySchemaError
    relationship rules   -> PolicyValidationError

This module only produces a validated, plain-data policy. Turning it
into VSL-Core constructs happens in vsl.policy. Nothing in here executes
anything found in the file: YAML is parsed with a safe loader, and
interpolation only substitutes environment variable text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator

from ..exceptions import (
    PolicyConfigError,
    PolicySchemaError,
    PolicyValidationError,
)

SCHEMA_PATH = Path(__file__).with_name("schema.json")

SUPPORTED_SCHEMA_VERSIONS = (1,)

# A policy file is a few KiB. Refuse anything absurd rather than parse it.
MAX_POLICY_BYTES = 1024 * 1024

# Keys that must never appear anywhere in a policy file (guide 12.3 rule 6).
FORBIDDEN_KEYS = frozenset({"api_key", "password", "secret", "token"})

_ENV_EXPR = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


# =====================================================================
# RESULT
# =====================================================================


@dataclass(frozen=True)
class PolicyConfig:
    """A fully loaded, interpolated and validated policy document.

    `data` is plain JSON-compatible data. `policy_hash` is the SHA-256
    of its canonical JSON form, so it reflects the policy actually in
    force after environment interpolation.
    """

    data: dict[str, Any]
    policy_hash: str
    source: str


# =====================================================================
# SCHEMA
# =====================================================================


def load_schema() -> dict[str, Any]:
    """Return the JSON Schema describing the supported policy format."""

    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


# =====================================================================
# SAFE YAML
# =====================================================================


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys.

    PyYAML silently keeps the last of two identical keys, which would
    let a stray second `max:` quietly override a reviewed first one.
    """


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)

    mapping: dict[Any, Any] = {}

    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)

        try:
            hash(key)
        except TypeError:
            raise PolicyConfigError(
                f"Unsupported YAML key at line "
                f"{key_node.start_mark.line + 1}."
            ) from None

        if key in mapping:
            raise PolicyConfigError(
                f"Duplicate key {key!r} at line "
                f"{key_node.start_mark.line + 1}."
            )

        mapping[key] = loader.construct_object(
            value_node,
            deep=deep,
        )

    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _parse_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_UniqueKeySafeLoader)

    except PolicyConfigError:
        raise

    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else ""
        )
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise PolicyConfigError(
            f"Malformed YAML{where}: {problem}."
        ) from None


# =====================================================================
# FORBIDDEN KEYS / KEY TYPES
# =====================================================================


def _check_keys(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PolicyConfigError(
                    f"Non-string key {key!r} at "
                    f"{path or '<root>'}."
                )

            normalised = key.strip().lower().replace("-", "_")

            if normalised in FORBIDDEN_KEYS:
                raise PolicyConfigError(
                    f"Forbidden key {key!r} at "
                    f"{path or '<root>'}: secrets must never "
                    "appear in a policy file."
                )

            _check_keys(child, f"{path}.{key}" if path else key)

    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_keys(child, f"{path}[{index}]")


# =====================================================================
# ENVIRONMENT INTERPOLATION
# =====================================================================


def _coerce_scalar(text: str) -> Any:
    """Type a whole-value interpolation result.

    Deliberately narrow: bool, int and finite float only. Everything
    else stays a string. This is not YAML parsing and cannot produce
    lists, mappings or anything executable.
    """

    if text == "true":
        return True

    if text == "false":
        return False

    if _INT.match(text):
        return int(text)

    if _FLOAT.match(text):
        value = float(text)

        if math.isfinite(value):
            return value

    return text


def interpolate_env(
    value: str,
    env: Mapping[str, str] | None = None,
    *,
    coerce: bool = False,
    where: str = "value",
) -> Any:
    """Substitute ``${VAR}`` and ``${VAR:-default}`` in one string.

    Rules (deterministic, single pass):

    - a variable that is unset OR set to the empty string counts as
      missing: ``${VAR}`` then raises, ``${VAR:-d}`` yields ``d``
    - an explicit empty default (``${VAR:-}``) yields ``""``
    - substituted text is never expanded again
    - a ``${`` that is not valid syntax raises
    - with ``coerce=True`` and a value that is exactly one expression,
      the result is typed as bool / int / finite float when it looks
      like one; mixed text always stays a string
    """

    source = os.environ if env is None else env

    remainder = _ENV_EXPR.sub("", value)

    if "${" in remainder:
        raise PolicyConfigError(
            f"Invalid environment interpolation syntax in {where}."
        )

    def substitute(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        current = source.get(name)

        if current is None or current == "":
            if default is None:
                raise PolicyConfigError(
                    f"Environment variable {name} is unset or "
                    f"empty and has no default (in {where})."
                )
            return default

        return current

    result = _ENV_EXPR.sub(substitute, value)

    whole = _ENV_EXPR.fullmatch(value) is not None

    if coerce and whole:
        return _coerce_scalar(result)

    return result


def _interpolate_mapping(
    mapping: dict[str, Any],
    env: Mapping[str, str] | None,
    where: str,
    *,
    coerce_keys: frozenset[str] | None,
) -> None:
    for key, value in list(mapping.items()):
        if isinstance(value, str):
            coerce = coerce_keys is None or key in coerce_keys
            mapping[key] = interpolate_env(
                value,
                env,
                coerce=coerce,
                where=f"{where}.{key}",
            )


def _interpolate(
    data: dict[str, Any],
    env: Mapping[str, str] | None,
) -> None:
    """Interpolate only where the guide allows it: `ledger.*` and
    construct `params`. Names, bindings and assurance facts are never
    environment-dependent.
    """

    ledger = data.get("ledger")

    if isinstance(ledger, dict):
        _interpolate_mapping(
            ledger,
            env,
            "ledger",
            coerce_keys=frozenset({"fsync"}),
        )

    for section in ("prenodes", "invariants"):
        items = data.get(section)

        if not isinstance(items, list):
            continue

        for index, item in enumerate(items):
            params = (
                item.get("params")
                if isinstance(item, dict)
                else None
            )

            if isinstance(params, dict):
                _interpolate_mapping(
                    params,
                    env,
                    f"{section}[{index}].params",
                    coerce_keys=None,
                )


# =====================================================================
# SCHEMA VALIDATION
# =====================================================================


def _format_path(error: Any) -> str:
    parts: list[str] = []

    for part in error.absolute_path:
        if isinstance(part, int):
            parts.append(f"[{part}]")
        else:
            parts.append(f".{part}" if parts else str(part))

    return "".join(parts) or "<root>"


def validate_schema(data: Any) -> None:
    """Validate against the JSON Schema; raise PolicySchemaError."""

    validator = Draft202012Validator(load_schema())

    errors = sorted(
        validator.iter_errors(data),
        key=lambda e: (list(map(str, e.absolute_path)), e.message),
    )

    if errors:
        raise PolicySchemaError(
            [f"{_format_path(e)}: {e.message}" for e in errors]
        )


# =====================================================================
# RELATIONSHIP VALIDATION
# =====================================================================


def _duplicates(names: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []

    for name in names:
        if name in seen and name not in dupes:
            dupes.append(name)

        seen.add(name)

    return dupes


def validate_relationships(data: dict[str, Any]) -> None:
    """Semantic rules a schema cannot express (guide 12.3).

    Collects every problem, then raises one PolicyValidationError.
    """

    errors: list[str] = []

    bases = set(data["assurance_bases"])
    terminals = [t["name"] for t in data["terminal_states"]]
    prenodes = data.get("prenodes", [])
    invariants = data.get("invariants", [])

    for kind, names in (
        ("terminal state", terminals),
        ("prenode", [p["name"] for p in prenodes]),
        ("invariant", [i["name"] for i in invariants]),
    ):
        for name in _duplicates(names):
            errors.append(f"Duplicate {kind} name {name!r}.")

    for index, prenode in enumerate(prenodes):
        if prenode["assurance_basis"] not in bases:
            errors.append(
                f"prenodes[{index}].assurance_basis: unknown "
                f"assurance basis {prenode['assurance_basis']!r}."
            )

    for index, invariant in enumerate(invariants):
        if invariant["assurance_basis"] not in bases:
            errors.append(
                f"invariants[{index}].assurance_basis: unknown "
                f"assurance basis {invariant['assurance_basis']!r}."
            )

        if invariant["on_violation"] not in terminals:
            errors.append(
                f"invariants[{index}].on_violation: "
                f"{invariant['on_violation']!r} is not a declared "
                "terminal state."
            )

    prenode_names = {p["name"] for p in prenodes}
    invariant_names = {i["name"] for i in invariants}

    for action, spec in data["actions"].items():
        action_prenodes = spec.get("prenodes", [])
        action_invariants = spec.get("invariants", [])

        if not action_prenodes and not action_invariants:
            errors.append(
                f"actions.{action}: declares no gates; an action "
                "needs at least one prenode or invariant."
            )

        for name in action_prenodes:
            if name not in prenode_names:
                errors.append(
                    f"actions.{action}.prenodes: unknown "
                    f"prenode {name!r}."
                )

        for name in action_invariants:
            if name not in invariant_names:
                errors.append(
                    f"actions.{action}.invariants: unknown "
                    f"invariant {name!r}."
                )

    ledger = data.get("ledger")

    if ledger is not None:
        backend = ledger["backend"]

        if backend == "jsonl":
            if "path" not in ledger:
                errors.append(
                    "ledger.path: required when ledger.backend "
                    "is 'jsonl'."
                )
        else:
            for key in ("path", "fsync"):
                if key in ledger:
                    errors.append(
                        f"ledger.{key}: only valid when "
                        f"ledger.backend is 'jsonl', not "
                        f"{backend!r}."
                    )

    if errors:
        raise PolicyValidationError(errors)


# =====================================================================
# PUBLIC LOADERS
# =====================================================================


def _canonical_hash(data: dict[str, Any]) -> str:
    blob = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_policy_config(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    source: str = "<string>",
) -> PolicyConfig:
    """Parse and fully validate policy YAML text."""

    document = _parse_yaml(text)

    if document is None:
        raise PolicyConfigError("Policy is empty.")

    if not isinstance(document, dict):
        raise PolicyConfigError(
            "Policy must be a YAML mapping at the top level."
        )

    if not document:
        raise PolicyConfigError("Policy is empty.")

    _check_keys(document)

    if "assurance_bases" in document and isinstance(
        document["assurance_bases"], dict
    ):
        for basis_name, basis in document["assurance_bases"].items():
            if isinstance(basis, dict) and (
                "level" in basis or "assurance_level" in basis
            ):
                raise PolicyValidationError(
                    [
                        f"assurance_bases.{basis_name}: an assurance "
                        "level cannot be set. Supply f1_pre_commitment "
                        "and f2_modification; VSL-Core derives the "
                        "level."
                    ]
                )

    _interpolate(document, env)

    validate_schema(document)
    validate_relationships(document)

    return PolicyConfig(
        data=document,
        policy_hash=_canonical_hash(document),
        source=source,
    )


def load_policy_config(
    path: str | os.PathLike[str],
    *,
    env: Mapping[str, str] | None = None,
) -> PolicyConfig:
    """Read, parse and fully validate a policy file."""

    file_path = Path(path)

    if not file_path.exists():
        raise PolicyConfigError(
            f"Policy file not found: {file_path}"
        )

    if not file_path.is_file():
        raise PolicyConfigError(
            f"Policy path is not a file: {file_path}"
        )

    try:
        size = file_path.stat().st_size

        if size > MAX_POLICY_BYTES:
            raise PolicyConfigError(
                f"Policy file is too large ({size} bytes; limit "
                f"{MAX_POLICY_BYTES})."
            )

        text = file_path.read_bytes().decode("utf-8")

    except PolicyConfigError:
        raise

    except UnicodeDecodeError:
        raise PolicyConfigError(
            f"Policy file is not valid UTF-8: {file_path}"
        ) from None

    except OSError as exc:
        raise PolicyConfigError(
            f"Policy file could not be read: {file_path} "
            f"({exc.strerror or 'I/O error'})"
        ) from None

    return parse_policy_config(
        text,
        env=env,
        source=str(file_path),
    )
