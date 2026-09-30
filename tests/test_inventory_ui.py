"""Regression contracts from the real light/dark mobile browser audit."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


TEMPLATE = (Path(__file__).parents[1] / 'templates/inventory.html').read_text(encoding='utf-8')


def test_alpine_owns_initialization_and_cards_use_theme_tokens():
    assert 'x-data="inventoryApp()"' in TEMPLATE
    assert 'x-init="init()"' not in TEMPLATE
    assert 'background:#fff' not in TEMPLATE
    assert 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3' in TEMPLATE


def test_stock_initial_expressions_and_timer_lifecycle():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is only required for the optional JavaScript runtime check')
    source = re.search(r'<script>\s*(function inventoryApp\(\).*?)</script>', TEMPLATE, re.S).group(1)
    script = source + r'''
const assert = require('node:assert/strict');
const app = inventoryApp();
assert.deepEqual(app.wh.warehouses, []);
assert.equal(app.maxWhQty, 1);
assert.equal(app.totalWhQty, 1);
const timers = new Set();
global.setInterval = () => { const id = {}; timers.add(id); return id; };
global.clearInterval = id => timers.delete(id);
global.document = {removeEventListener() {}};
app.startAutoRefresh();
app.startAutoRefresh();
assert.equal(timers.size, 2, 'reinitialization must not duplicate timers');
app.destroy();
assert.equal(timers.size, 0, 'leaving the page must release timers');
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True, timeout=10)
