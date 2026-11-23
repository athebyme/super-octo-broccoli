"""Exact selected-only catalog review browser logic regressions."""

from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).parent / 'ux01' / 'my_products_selection_review.js'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is unavailable')
def test_my_products_selection_review_regressions():
    result = subprocess.run(
        ['node', str(SCRIPT)],
        cwd=SCRIPT.parents[2],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert 'selection review regressions passed' in result.stdout
