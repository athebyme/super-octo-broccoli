"""Pure source-photo identities shared by cached delivery and editor previews."""
import json


def photo_entry_urls(entry):
    if isinstance(entry, str):
        return entry, []
    if not isinstance(entry, dict):
        return None, []
    primary = entry.get('sexoptovik') or entry.get('original') or entry.get('blur')
    if not isinstance(primary, str) or not primary:
        return None, []
    fallbacks = []
    for key in ('blur', 'original'):
        candidate = entry.get(key)
        if isinstance(candidate, str) and candidate and candidate != primary and candidate not in fallbacks:
            fallbacks.append(candidate)
    return primary, fallbacks


def imported_photo_previews(source):
    """Match only URLs served by the same exact owned source/index route.

    Supplier slots take precedence over legacy imported slots, just as in the
    authenticated photo route. These display URLs never enter publication data.
    """
    def rows(value):
        try:
            value = json.loads(value or '[]')
            return value[:30] if isinstance(value, list) else []
        except (ValueError, TypeError):
            return []

    supplier = source.supplier_product
    supplier_rows = rows(supplier.photo_urls_json) if supplier else []
    imported_rows = rows(source.photo_urls)
    previews = {}
    for index in range(max(len(supplier_rows), len(imported_rows))):
        primary, fallbacks = photo_entry_urls(supplier_rows[index]) if index < len(supplier_rows) else (None, [])
        if not primary and index < len(imported_rows):
            primary, fallbacks = photo_entry_urls(imported_rows[index])
        for url in [primary, *fallbacks]:
            if isinstance(url, str) and url and len(url) <= 2000:
                previews.setdefault(url, f'/api/photos/imported-product/{source.id}/{index}?deferred=1')
    return previews
