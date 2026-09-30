from services.listing_navigation import (
    build_listing_catalog_url,
    current_listing_catalog_url,
    listing_return_url,
    validate_listing_return_url,
)


def test_listing_return_url_preserves_supported_catalog_context_and_canonicalizes():
    candidate = (
        "/marketplaces/listings/beta?marketplace=ozon&account_id=17&status=error"
        "&link_status=linked&include_unavailable=1&search=%D0%B4%D0%BE%D0%BC%2F%D0%B4%D0%B0"
        "&page=3&per_page=20"
    )

    safe = validate_listing_return_url(candidate)

    assert safe == candidate
    assert build_listing_catalog_url("/marketplaces/listings/classic", {
        "marketplace_code": "ozon",
        "account_id": 17,
        "normalized_status": "error",
        "link_status": "linked",
        "include_unavailable": True,
        "search": "дом/да",
        "page": 3,
        "per_page": 20,
    }) == (
        "/marketplaces/listings/classic?marketplace=ozon&account_id=17&status=error"
        "&link_status=linked&include_unavailable=1&search=%D0%B4%D0%BE%D0%BC%2F%D0%B4%D0%B0"
        "&page=3&per_page=20"
    )


def test_return_target_rejects_non_catalog_destinations_and_ambiguous_queries():
    rejected = [
        "https://example.test/marketplaces/listings/",
        "//example.test/marketplaces/listings/",
        "\\\\example.test\\marketplaces\\listings\\",
        "/marketplaces/listings/%2fclassic",
        "/marketplaces/listings/%5cclassic",
        "/marketplaces/listings/%252fclassic",
        "/marketplaces/listings/../listings/classic",
        "/marketplaces/listings/?status=error&status=active",
        "/marketplaces/listings/?search=x&next=/admin",
        "/marketplaces/listings/?page=2&page=3",
        "/marketplaces/listings/?account_id=1%0d%0aLocation%3Ahttps%3A%2F%2Fevil.test",
        "/marketplaces/listings/?per_page=101",
        "/marketplaces/listings/?page=90071992547410",
        "/marketplaces/listings/?status=unknown-provider-state",
    ]

    assert all(validate_listing_return_url(value) is None for value in rejected)


def test_return_target_keeps_existing_case_insensitive_and_integer_filter_forms():
    assert validate_listing_return_url(
        "/marketplaces/listings/?marketplace=%20OZON%20&account_id=00017"
        "&include_unavailable=true&page=003&per_page=020"
    ) == (
        "/marketplaces/listings/?marketplace=ozon&account_id=17"
        "&include_unavailable=1&page=3&per_page=20"
    )
    assert validate_listing_return_url(
        "/marketplaces/listings/?search=backslash%5Cvalue"
    ) == "/marketplaces/listings/?search=backslash%5Cvalue"
    assert validate_listing_return_url(
        "/marketplaces/listings/?per_page=0001&page=0003"
    ) == "/marketplaces/listings/?per_page=1&page=3"


def test_invalid_or_duplicate_return_to_uses_exact_listing_scope_fallback():
    assert listing_return_url(
        ["/marketplaces/listings/classic?search=x&search=y"],
        marketplace_code="ozon",
        account_id=42,
    ) == "/marketplaces/listings/?marketplace=ozon&account_id=42"
    assert listing_return_url(
        ["/marketplaces/listings/?status=error", "/marketplaces/listings/?status=active"],
        marketplace_code="wb",
    ) == "/marketplaces/listings/?marketplace=wb"
    assert listing_return_url([], marketplace_code="ozon") == "/marketplaces/listings/?marketplace=ozon"


def test_current_catalog_url_drops_unrecognized_context_and_keeps_safe_route():
    assert current_listing_catalog_url(
        "/marketplaces/listings/classic",
        "marketplace=wb&page=4&per_page=50&debug=1",
        fallback_filters={"marketplace_code": "wb", "page": 4, "per_page": 50},
    ) == "/marketplaces/listings/classic?marketplace=wb&page=4&per_page=50"
    assert current_listing_catalog_url(
        "/elsewhere",
        "marketplace=ozon",
        fallback_filters={"marketplace_code": "ozon", "account_id": 42},
    ) == "/marketplaces/listings/?marketplace=ozon&account_id=42"
