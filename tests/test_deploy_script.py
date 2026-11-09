"""infra/deploy.sh has to stay runnable on the machine that actually deploys.

These cannot call GCP and should not. What they can do is stop the script failing
for reasons that have nothing to do with the deploy.

The bug they exist for: the preflight ran `python -m pytest`. macOS ships no
`python`, only `python3`, so on the owner's laptop the shell exited 127 with
"command not found" before a single test ran. The script treats any non-zero exit
as a test failure, so it printed "tests failed, refusing to deploy" and stopped.

Every deploy was refused for weeks while the message blamed the test suite, and
the scanner kept serving an image from before two merged fixes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "infra" / "deploy.sh"
TEXT = SCRIPT.read_text()


def test_the_script_is_valid_shell():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    subprocess.run([bash, "-n", str(SCRIPT)], check=True)


def test_no_bare_python_call_remains():
    """A bare `python` is the bug. Only `python3` or "$PYTHON" may run anything."""
    bare = [
        line
        for line in TEXT.splitlines()
        if not line.lstrip().startswith("#")
        and not line.lstrip().startswith("PYTHON=")  # the lookup itself, not a call
        and re.search(r'(?<![\w3"$/])python(?![\w3])', line)
    ]
    assert bare == [], f"bare `python` call(s) in deploy.sh: {bare}"


def test_the_interpreter_is_resolved_before_the_preflight_uses_it():
    """Resolving after the first use would reintroduce the bug for that line."""
    assign = TEXT.index("PYTHON=")
    assert assign < TEXT.index('"$PYTHON" -m pytest')
    assert assign < TEXT.index('"$PYTHON" infra/preflight.py')


def test_a_missing_interpreter_says_so_instead_of_blaming_the_tests():
    """The failure mode that cost the weeks: the wrong message for the real cause."""
    assert "no python3 on PATH" in TEXT


def test_preflight_still_refuses_the_deploy_when_tests_fail():
    """The guard this script exists for has to survive the fix."""
    assert "refusing to deploy" in TEXT
    assert "SKIP_PREFLIGHT" in TEXT
