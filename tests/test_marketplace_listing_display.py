import json

import pytest

from models import MarketplaceListing
from services.marketplace_listing_display import listing_display


def display(price=None, stock=None, media=None):
    row = MarketplaceListing(price_summary_json=json.dumps(price or {}),
                             stock_summary_json=json.dumps(stock or {}), media_json=json.dumps(media or {}))
    return listing_display(row)


@pytest.mark.parametrize('price,expected', [
    ({'values': {'price': '1299.50'}, 'currency': 'RUB'}, '1\u00a0299,50 ₽'),
    ({'values': {'price': '1200', 'marketing_seller_price': '0'}, 'currency': 'RUB'}, '1\u00a0200 ₽'),
    ({'values': {'price': '1200', 'marketing_seller_price': '900'}, 'currency': 'RUB'}, '1\u00a0200 ₽'),
    ({'values': {'marketing_seller_price': '900'}, 'currency': 'RUB'}, '—'),
    ({'values': {'price': '1200', 'marketing_seller_price': None}, 'currency': 'USD'}, '1\u00a0200 USD'),
    ({'price': 1000, 'discount_price': 900, 'source': 'legacy_wb_projection'}, '900 ₽'),
    ({'price': 1000, 'discount_price': 0, 'source': 'legacy_wb_projection'}, '0 ₽'),
    ({'values': {'price': 'NaN'}}, '—'),
    ({'values': {'price': 'Infinity'}}, '—'),
    ({'values': {'price': True}}, '—'),
    ({'values': {'price': '-1'}}, '—'),
    ({'values': {'price': '123bad'}}, '—'),
    ({'available': False, 'values': {'price': '1200'}}, '—'),
    ({}, '—'),
])
def test_observed_price_lanes_and_unknowns(price, expected):
    assert display(price=price)['price'] == expected


def test_base_seller_and_promotion_remain_distinct_without_invented_buyer_price():
    result = display(price={'currency': 'RUB', 'values': {
        'old_price': '1462', 'price': '1059', 'marketing_seller_price': '900',
        'marketing_price': '800', 'retail_price': '700',
    }})
    assert result['base_price'] == '1\u00a0462 ₽'
    assert result['price'] == '1\u00a0059 ₽'
    assert result['promotion_price'] == '900 ₽'
    assert result['buyer_price'] is None and result['marketplace_discount'] is None
    for bad in [0, '0.00', None, '', True, '-1', 'NaN']:
        result = display(price={'currency': 'RUB', 'values': {
            'old_price': bad, 'price': '1059', 'marketing_seller_price': bad,
        }})
        assert result['base_price'] == result['promotion_price'] == '—'
        assert not result['has_promotion_price']
        assert result['price'] == '1\u00a0059 ₽'


def test_unavailable_or_malformed_summary_does_not_show_old_or_promotion_as_current():
    result = display(price={'available': False, 'currency': 'RUB', 'values': {
        'old_price': '1400', 'price': '1000', 'marketing_seller_price': '900',
    }})
    assert result['base_price'] == result['price'] == result['promotion_price'] == '—'
    for malformed in ['not-an-object', ['wrong']]:
        assert display(price=malformed)['price'] == '—'


@pytest.mark.parametrize('stock,expected', [({}, None), ({'present': 0}, 0), ({'present': 12}, 12),
    ({'available': False, 'present': 0}, None), ({'present': True}, None), ({'present': '0'}, None),
    ({'present': -1}, None)])
def test_stock_zero_is_not_unknown(stock, expected):
    assert display(stock=stock)['stock'] == expected


def test_only_observed_http_image_is_used():
    assert display(media={'primary_image': 'javascript:alert(1)'})['image'] is None
    assert display(media={'primary_image': 'https://img.example.test/a.jpg'})['image'] == 'https://img.example.test/a.jpg'
