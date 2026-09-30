"""The login form preserves destination; browser-special separators stay local."""
from pathlib import Path
import pytest
from services.url_security import is_safe_local_path


@pytest.mark.parametrize('value',[
    None, '', 'https://outside.example', '//outside.example', 'javascript:alert(1)',
    '/\\outside.example', '/%5coutside.example', '/%2foutside.example',
    '/\n/outside.example', '/%09/outside.example', '/%ff',
])
def test_unsafe_login_destination_is_rejected(value):
    assert not is_safe_local_path(value)


@pytest.mark.parametrize('value',[
    '/', '/marketplaces/quality?account_id=1&page=2',
    '/marketplaces/listings/?search=%D0%A2%D0%B5%D1%81%D1%82',
    '/marketplaces/quality?search=https://example.test#details',
])
def test_local_account_filter_and_query_survive(value):
    assert is_safe_local_path(value)


def test_form_submits_next_to_same_login_route():
    template=(Path(__file__).parents[1]/'templates/login.html').read_text()
    assert 'action="{{ url_for(\'login\', next=request.args.get(\'next\')) }}"' in template
