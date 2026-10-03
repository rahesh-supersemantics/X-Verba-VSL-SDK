"""
Phase 9 -- the X-Verba Scan boundary.

Scan (the ``x-verba`` tool) is a separate product. The SDK's only
integration is running it, unchanged, through ``vsl.integrations.scan`` and
``vsl scan``. These tests use a stand-in ``x-verba`` executable to check
the passthrough exactly; one test runs the real tool when it is installed.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vsl import SDKError
from vsl import cli
from vsl.integrations.scan import (
    SCANNER_COMMAND,
    ScannerNotFoundError,
    find_scanner,
    run_scan,
)

ROOT = Path(__file__).resolve().parent.parent

STAND_IN = """#!{python}
import json, sys
print(json.dumps(sys.argv[1:]))
sys.exit(int(__import__("os").environ.get("FAKE_SCAN_EXIT", "0")))
"""


@pytest.fixture
def fake_scanner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if os.name == "nt":
        pytest.skip("stand-in executable is a POSIX script")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / SCANNER_COMMAND
    exe.write_text(STAND_IN.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return exe


@pytest.fixture
def no_scanner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))


def test_scan_error_is_an_sdk_error() -> None:
    assert issubclass(ScannerNotFoundError, SDKError)


def test_find_scanner(fake_scanner: Path) -> None:
    assert find_scanner() == str(fake_scanner)


def test_run_scan_forwards_arguments_after_scan(fake_scanner: Path, capfd: Any) -> None:
    assert run_scan(["--format", "json", "src/"]) == 0

    assert json.loads(capfd.readouterr().out) == ["scan", "--format", "json", "src/"]


def test_run_scan_returns_the_scanners_exit_status_unchanged(
    fake_scanner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SCAN_EXIT", "7")

    assert run_scan([]) == 7


def test_run_scan_without_the_scanner_raises_with_an_install_hint(no_scanner: None) -> None:
    assert find_scanner() is None

    with pytest.raises(ScannerNotFoundError, match=r"\[scan\]"):
        run_scan([])


def test_cli_scan_passes_everything_through_including_flags(
    fake_scanner: Path, capfd: Any
) -> None:
    # '--help' and '--ledger' belong to the scanner here, not to vsl.
    assert cli.main(["scan", "--help", "--ledger", "x"]) == 0

    assert json.loads(capfd.readouterr().out) == ["scan", "--help", "--ledger", "x"]


def test_cli_scan_exit_status_is_the_scanners(
    fake_scanner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SCAN_EXIT", "1")

    assert cli.main(["scan", "."]) == 1


def test_cli_scan_without_the_scanner_exits_127(no_scanner: None, capsys: Any) -> None:
    assert cli.main(["scan"]) == cli.EXIT_SCAN_UNAVAILABLE == 127

    assert "not installed" in capsys.readouterr().err


def test_cli_still_lists_scan_in_help_and_keeps_other_commands(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])

    assert "scan" in capsys.readouterr().out
    assert cli.main(["verify"]) == cli.EXIT_USAGE  # missing --ledger, unchanged


def test_the_sdk_does_not_import_the_scanner() -> None:
    code = (
        "import sys, vsl, vsl.cli, vsl.integrations.scan; "
        "assert 'x_verba' not in sys.modules"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr


def test_scan_output_is_not_ledger_evidence(fake_scanner: Path, tmp_path: Path) -> None:
    """Running a scan creates and changes nothing in a ledger."""

    ledger = tmp_path / "ledger.jsonl"
    before = sorted(p.name for p in tmp_path.iterdir())

    run_scan([])

    assert not ledger.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.skipif(shutil.which(SCANNER_COMMAND) is None, reason="x-verba (X-Verba Scan) is not installed")
def test_the_real_scanner_runs(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "vsl.cli", "scan", "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    assert done.returncode == 0
    assert "scan" in (done.stdout + done.stderr).lower()
