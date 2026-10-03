"""Helpers for testing YAML policies: valid, rejected, and safe."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from vsl.exceptions import SDKError
from vsl.governance import Governance
from vsl.ledger import memory_ledger

from .policies import write_policy


def load_policy_text(
    directory: str | Path,
    text: str,
    *,
    env: Mapping[str, str] | None = None,
) -> Governance:
    """Load policy text through the real ``Governance.from_yaml`` with an
    in-memory ledger and a hermetic environment."""

    return Governance.from_yaml(
        write_policy(directory, text),
        ledger=memory_ledger(),
        env={} if env is None else env,
    )


def policy_error(
    directory: str | Path,
    text: str,
    *,
    env: Mapping[str, str] | None = None,
) -> SDKError:
    """Load policy text that must be rejected; return the SDK error.

    Raises ``AssertionError`` if the policy is accepted, and lets a
    non-SDK exception (a bug, not a rejection) escape unchanged.
    """

    try:
        load_policy_text(directory, text, env=env)
    except SDKError as exc:
        return exc

    raise AssertionError("the policy was accepted but should be rejected")


def replace_in_policy(base: str, old: str, new: str) -> str:
    """Derive a variant of a policy; ``old`` must occur exactly once."""

    if base.count(old) != 1:
        raise AssertionError(
            f"{old!r} occurs {base.count(old)} times in the base policy"
        )

    return base.replace(old, new)


def error_messages(error: Any) -> list[str]:
    """All messages carried by a policy error (``.errors`` or ``str``)."""

    errors = getattr(error, "errors", None)

    return [str(e) for e in errors] if errors else [str(error)]
