from pathlib import Path


def test_web_healthcheck_bounds_and_closes_urllib_request():
    dockerfile = (Path(__file__).parents[1] / 'Dockerfile').read_text(encoding='utf-8')
    healthcheck = next(
        line for line in dockerfile.splitlines()
        if 'urllib.request.urlopen' in line
    )

    assert 'timeout=3' in healthcheck
    assert 'response.close()' in healthcheck
