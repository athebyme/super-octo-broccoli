from pathlib import Path


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
