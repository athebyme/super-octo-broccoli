"""Strict parsing, filter identity, and safe return handling for WB selections."""

import pytest
from werkzeug.datastructures import MultiDict

from services.product_selection import (
    MAX_PRODUCT_LIST_PAGE,
    MAX_SIGNED_SQLITE_ID,
    ProductSelectionError,
    parse_product_list_state,
    parse_selected_product_ids,
    product_filter_fingerprint,
    product_list_url_args,
    safe_products_return_url,
)


def test_selected_ids_reject_duplicates_foreign_shapes_and_over_cap():
    assert parse_selected_product_ids([7, 2]) == [7, 2]
    for bad in ([7, 7], [True], [2.0], ['7'], [0], [-1], []):
        with pytest.raises(ProductSelectionError):
            parse_selected_product_ids(bad)
    with pytest.raises(ProductSelectionError, match='200'):
        parse_selected_product_ids(list(range(1, 202)))


def test_filter_fingerprint_ignores_sort_and_page_but_tracks_filters():
    first = parse_product_list_state({
        'brand': 'Pipedream', 'sort': 'updated_at', 'order': 'desc', 'page': 1,
    }, strict=True)
    other_page_and_sort = parse_product_list_state({
        'brand': 'Pipedream', 'sort': 'title', 'order': 'asc', 'page': 3,
    }, strict=True)
    changed_filter = parse_product_list_state({
        'brand': 'Pipedream Classic', 'sort': 'updated_at', 'order': 'desc', 'page': 1,
    }, strict=True)
    assert product_filter_fingerprint(4, first['filters']) == product_filter_fingerprint(
        4, other_page_and_sort['filters'],
    )
    assert product_filter_fingerprint(4, first['filters']) != product_filter_fingerprint(
        4, changed_filter['filters'],
    )


def test_return_url_parser_keeps_encoded_separators_inside_search_value():
    safe = safe_products_return_url(
        '/products?search=Pipedream%26category%3Dwrong&page=2&sort=title&order=asc'
    )
    assert safe == (
        '/products?sort=title&order=asc&per_page=50&search=Pipedream%26category%3Dwrong&page=2'
    )
    for unsafe in (
        'https://evil.example/products',
        '//evil.example/products',
        '/products?search=x&%2f%2fevil=1',
        '/products?search=x&search=y',
        '/products#external',
        '/products\\evil',
        '/other?next=/products',
    ):
        assert safe_products_return_url(unsafe) == '/products'


def test_strict_filters_reject_unknown_and_duplicate_query_keys():
    with pytest.raises(ProductSelectionError):
        parse_product_list_state({'brand': 'Pipedream', 'owner': 'other'}, strict=True, reject_unknown=True)
    with pytest.raises(ProductSelectionError):
        parse_product_list_state(
            MultiDict([('brand', 'Pipedream'), ('brand', 'Other')]), strict=True,
        )


def test_return_state_preserves_numeric_zero_filter_values():
    state = parse_product_list_state({'rating_min': '0'}, strict=True)
    assert state['filters']['rating_min'] == 0.0
    assert product_list_url_args(state)['rating_min'] == 0.0
    assert 'rating_min=0.0' in safe_products_return_url('/products?rating_min=0')


def test_selection_and_numeric_controls_reject_values_outside_sqlite_bounds():
    assert parse_selected_product_ids([MAX_SIGNED_SQLITE_ID]) == [MAX_SIGNED_SQLITE_ID]
    assert parse_selected_product_ids([str(MAX_SIGNED_SQLITE_ID)], from_query=True) == [
        MAX_SIGNED_SQLITE_ID,
    ]
    for raw_ids, from_query in (
        ([MAX_SIGNED_SQLITE_ID + 1], False),
        ([str(MAX_SIGNED_SQLITE_ID + 1)], True),
        (['9' * 5000], True),
        (['0' * 20 + '7'], True),
    ):
        with pytest.raises(ProductSelectionError):
            parse_selected_product_ids(raw_ids, from_query=from_query)

    with pytest.raises(ProductSelectionError, match='страницы'):
        parse_product_list_state({'page': str(MAX_PRODUCT_LIST_PAGE + 1)}, strict=True)
    with pytest.raises(ProductSelectionError):
        parse_product_list_state({'page': '9' * 5000}, strict=True)
    with pytest.raises(ProductSelectionError, match='страницы'):
        parse_product_list_state({'per_page': '201'}, strict=True)
    assert safe_products_return_url(
        f'/products?page={MAX_PRODUCT_LIST_PAGE + 1}'
    ) == '/products'
