"""SDK exceptions.

Every SDK-originated failure derives from SDKError so callers can catch
the family. Governance failures raised by VSL-Core (for example
AutomationDeniedException, InvariantViolation, LedgerIntegrityError) are
never wrapped or swallowed by the SDK.
"""

from __future__ import annotations


class SDKError(Exception):
    """Base class for all X-Verba VSL SDK errors."""


# ---------------------------------------------------------------------
# Phase 3 — policy configuration
# ---------------------------------------------------------------------


class PolicyConfigError(SDKError):
    """The policy file could not be loaded as a policy.

    Missing or unreadable file, malformed YAML, empty document,
    duplicate keys, forbidden keys, bad environment interpolation.
    """


class PolicySchemaError(PolicyConfigError):
    """The policy does not match the JSON Schema (structure, required
    fields, field types, unsupported values).

    `errors` lists "<path>: <message>" strings, one per problem.
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            "Policy does not match the schema:\n  - "
            + "\n  - ".join(self.errors)
        )


class PolicyValidationError(PolicyConfigError):
    """The policy is structurally valid but semantically invalid:
    unknown references, missing terminal states, bad construct
    parameters, and similar relationship errors.

    `errors` lists one string per problem.
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            "Policy is invalid:\n  - " + "\n  - ".join(self.errors)
        )


class RegistryError(SDKError):
    """Monitor/rule registry or policy registry misuse: unknown name,
    duplicate registration, ambiguous version.
    """


# ---------------------------------------------------------------------
# Phase 4 — ledger integration
# ---------------------------------------------------------------------


class LedgerStorageError(SDKError):
    """The ledger could not be opened or read: missing file, malformed
    JSONL, structurally invalid entries, remote backend unavailable or
    misconfigured.
    """


class LedgerVerificationError(SDKError):
    """A ledger failed an integrity, audit, causal or certification
    requirement where the caller asked for an exception.
    """


class CheckpointError(SDKError):
    """A checkpoint could not be exported, read or parsed (as opposed
    to a checkpoint that parsed fine but does not match the ledger,
    which is reported as a CheckpointVerification result).
    """


class CLIUsageError(SDKError):
    """Invalid command-line usage."""
