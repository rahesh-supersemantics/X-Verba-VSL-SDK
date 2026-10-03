"""Optional pytest fixtures. Opt in from a ``conftest.py``:

    pytest_plugins = ["vsl.testing.pytest_plugin"]

Nothing here is registered automatically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .harness import GovernedHarness, build_harness


@pytest.fixture
def vsl_harness(tmp_path: Path) -> GovernedHarness:
    """Governance over the reference policy and a JSONL ledger file."""

    return build_harness(tmp_path)


@pytest.fixture
def vsl_memory_harness(tmp_path: Path) -> GovernedHarness:
    """Governance over the reference policy and an in-memory ledger."""

    return build_harness(tmp_path, ledger="memory")
