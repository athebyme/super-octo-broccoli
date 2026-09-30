"""Local presentation of observed catalog facts; never fetch or infer stock."""

from decimal import Decimal, InvalidOperation


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if len(str(value)) > 40:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number > 10**15:
        return None
    return number


def listing_display(listing):
    """Both Ozon nested prices and the supported legacy WB flat projection."""
    price = listing._json_value(listing.price_summary_json, {})
    stock = listing._json_value(listing.stock_summary_json, {})
    price = price if isinstance(price, dict) else {}
    stock = stock if isinstance(stock, dict) else {}
    values = price.get('values')
    currency = price.get('currency')
    if isinstance(currency, str) and currency.isascii() and currency.isalpha() and len(currency) == 3:
        currency = currency.upper()
    current = None
    base = None
    promotion = None
    if price.get('available') is not False:
        if isinstance(values, dict):
            current = _money(values.get('price'))
            base = _money(values.get('old_price'))
            promotion = _money(values.get('marketing_seller_price'))
            base = base if base is not None and base > 0 else None
            promotion = promotion if promotion is not None and promotion > 0 else None
        else:
            current = _money(price.get('discount_price'))
            if current is None:
                current = _money(price.get('price'))
            if price.get('source') == 'legacy_wb_projection' and not currency:
                currency = 'RUB'
    def label_for(amount):
        if amount is None:
            return '—'
        label = format(amount, ',.2f').replace(',', '\u00a0').replace('.', ',')
        if label.endswith(',00'):
            label = label[:-3]
        if currency == 'RUB':
            label += ' ₽'
        elif isinstance(currency, str) and currency.isascii() and currency.isalpha() and len(currency) == 3:
            label += ' ' + currency.upper()
        return label
    present = stock.get('present')
    if stock.get('available') is False or type(present) is not int or present < 0:
        present = None
    image = listing.primary_image_url()
    if not isinstance(image, str) or not image.startswith(('https://', 'http://')):
        image = None
    return {'price': label_for(current), 'stock': present, 'image': image,
            'base_price': label_for(base), 'promotion_price': label_for(promotion),
            'has_promotion_price': promotion is not None,
            'buyer_price': None, 'marketplace_discount': None}
