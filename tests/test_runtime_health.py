from pathlib import Path

import ast

import pytest
from flask import Flask, request


def test_web_healthcheck_bounds_and_closes_urllib_request():
    dockerfile = (Path(__file__).parents[1] / 'Dockerfile').read_text(encoding='utf-8')
    declaration = next(
        line for line in dockerfile.splitlines()
        if line.startswith('HEALTHCHECK ')
    )
    healthcheck = next(
        line for line in dockerfile.splitlines()
        if 'urllib.request.urlopen' in line
    )

    # 600s: fail-fast startup-миграции на многогигабайтной проде занимали 451s
    assert '--start-period=600s' in declaration
    assert 'timeout=3' in healthcheck
    assert 'response.close()' in healthcheck


@pytest.mark.parametrize('endpoint,policy,status,expected_private,expected_public', [
    ('ozon_account_health.api', 'private, no-store', 200, True, False),
    ('ozon_account_health.api', 'private, no-store', 404, True, False),
    ('ordinary', '', 200, False, False),
    ('ordinary', 'public, max-age=3600', 200, False, False),
    ('marketplace_image_asset', 'public, max-age=3600', 200, False, True),
    ('marketplace_image_asset', '', 403, False, False),
])
def test_global_response_policy_keeps_private_observations_and_asset_boundary(
    endpoint, policy, status, expected_private, expected_public
):
    # Execute the production hook without booting schedulers or migration/runtime setup.
    source = ast.parse((Path(__file__).parents[1] / 'seller_platform.py').read_text())
    hook = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'after_request')
    hook.decorator_list = []
    scope = {'request': request}
    exec(compile(ast.Module(body=[hook], type_ignores=[]), 'seller_platform.py', 'exec'), scope)
    app = Flask(__name__)
    app.add_url_rule('/', endpoint, lambda: '')
    with app.test_request_context('/'):
        response = app.response_class('{}', status=status, content_type='application/json')
        if policy:
            response.headers['Cache-Control'] = policy
        result = scope['after_request'](response)
        assert bool(result.cache_control.private) is expected_private
        assert bool(result.cache_control.public) is expected_public
        assert bool(result.cache_control.no_store) is not expected_public
        assert result.status_code == status
