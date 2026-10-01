"""Run a tiny Node DOM harness against the shipped inline selection controller."""

from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).parent / 'ux01' / 'wb_product_selection_dom.js'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is unavailable')
def test_product_selection_dom_regressions():
    result = subprocess.run(
        ['node', str(SCRIPT)],
        cwd=SCRIPT.parents[2],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert 'DOM regressions passed' in result.stdout
