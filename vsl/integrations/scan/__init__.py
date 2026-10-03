"""Boundary with X-Verba Scan (the ``x-verba`` command line tool).

X-Verba Scan is a separate Super Semantics product: a static analyser that
reads a *codebase* and reports governance gaps. This SDK governs a running
application and keeps its evidence in a ledger. They answer different
questions and share no data model, so this package does not import
``x_verba`` and does not reproduce any analysis. It offers the one
composition point that is true to both: running the scanner.

``run_scan`` executes ``x-verba scan <args...>`` as a subprocess, unchanged,
and returns its exit status. What the scanner writes (for example under
``.verba/``) is its own output. It is not ledger evidence and has no effect
on ``verify_integrity``, ``audit`` or certification.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Sequence

from vsl.integrations.common import IntegrationError

SCANNER_COMMAND = "x-verba"

INSTALL_HINT = "pip install 'super-semantics-vsl-sdk[scan]'"


class ScannerNotFoundError(IntegrationError):
    """The ``x-verba`` executable is not on PATH."""


def find_scanner() -> str | None:
    """Path of the ``x-verba`` executable, or ``None`` if not installed."""

    return shutil.which(SCANNER_COMMAND)


def run_scan(args: Sequence[str]) -> int:
    """Run ``x-verba scan <args...>`` and return its exit status unchanged.

    The scanner inherits stdin, stdout and stderr. Raises
    :class:`ScannerNotFoundError` if ``x-verba`` is not installed.
    """

    executable = find_scanner()

    if executable is None:
        raise ScannerNotFoundError(
            f"X-Verba Scan ('{SCANNER_COMMAND}') is not installed. "
            f"Install it with: {INSTALL_HINT}"
        )

    return subprocess.run([executable, "scan", *args], check=False).returncode
