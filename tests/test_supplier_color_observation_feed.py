"""Read-only supplier color feed extraction uses configured source cells only."""

import csv
import hashlib
from io import StringIO
from types import SimpleNamespace

import pytest

from services.supplier_color_observation_feed import (
    SupplierColorFeedError,
    read_supplier_color_observations,
    supports_direct_color,
)


def _csv(rows):
    stream = StringIO()
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _mapping():
    return {
        "external_id": {"column": "source_id", "type": "string"},
        "title": {"column": "name", "type": "string"},
        "colors": {"column": "shade", "type": "list", "separator": "|"},
        "observed_supplier_fields_version": {"column": "forged_version", "type": "string"},
        "observed_supplier_fields": {"column": "forged_fields", "type": "string"},
    }


def _supplier(**overrides):
    values = {
        "id": 31,
        "code": "color-feed-test",
        "csv_source_url": "https://feed.example.test/catalog.csv",
        "csv_delimiter": ";",
        "csv_encoding": "utf-8",
        "csv_has_header": True,
        "csv_column_mapping": _mapping(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_reads_one_exact_feed_and_returns_only_direct_color_cells():
    body = _csv([
        ["source_id", "name", "shade", "forged_version", "forged_fields"],
        [
            "SKU-001", "Летняя рубашка", "белый|голубой", "77",
            '{"colors":["подделка"]}',
        ],
        ["SKU-002", "Платье", "", "88", '{"colors":["черный"]}'],
    ])
    calls = []

    result = read_supplier_color_observations(
        _supplier(),
        fetch_bytes=lambda url: calls.append(url) or body,
    )

    assert calls == ["https://feed.example.test/catalog.csv"]
    assert result.total_rows == 2
    assert result.colors_by_external_id == {"SKU-001": ("белый", "голубой")}
    assert result.body_sha256 == hashlib.sha256(body).hexdigest()


def test_rejects_duplicate_ids_before_returning_partial_observations():
    body = _csv([
        ["source_id", "name", "shade", "forged_version", "forged_fields"],
        ["same", "Товар 1", "красный", "", ""],
        ["same", "Товар 2", "синий", "", ""],
    ])

    with pytest.raises(SupplierColorFeedError) as caught:
        read_supplier_color_observations(_supplier(), fetch_bytes=lambda _url: body)

    assert caught.value.code == "invalid_feed"
    assert str(caught.value) == "Supplier color feed error: invalid_feed"


@pytest.mark.parametrize("body", [b"", b"source_id;name;shade\n1;\xff;red\n"])
def test_rejects_empty_and_undecodable_feed(body):
    with pytest.raises(SupplierColorFeedError) as caught:
        read_supplier_color_observations(_supplier(), fetch_bytes=lambda _url: body)
    assert caught.value.code == "invalid_feed"


def test_rejects_malformed_quoted_csv():
    body = b'source_id;name;shade\nSKU-1;"unterminated;red\n'
    with pytest.raises(SupplierColorFeedError) as caught:
        read_supplier_color_observations(_supplier(), fetch_bytes=lambda _url: body)
    assert caught.value.code == "invalid_feed"


def test_rejects_a_row_the_parser_cannot_preserve_by_exact_id_and_title():
    body = _csv([
        ["source_id", "name", "shade", "forged_version", "forged_fields"],
        ["SKU-001", "", "белый", "", ""],
    ])
    with pytest.raises(SupplierColorFeedError) as caught:
        read_supplier_color_observations(_supplier(), fetch_bytes=lambda _url: body)
    assert caught.value.code == "invalid_feed"


def test_unsupported_or_ambiguous_configuration_never_fetches():
    calls = []
    unsupported = _supplier(csv_column_mapping={
        "external_id": {"column": "id", "type": "string"},
        "title": {"column": "title", "type": "string"},
        "colors": {"column": "shade", "type": "string"},
    })

    assert not supports_direct_color(unsupported)
    with pytest.raises(SupplierColorFeedError) as caught:
        read_supplier_color_observations(
            unsupported,
            fetch_bytes=lambda url: calls.append(url) or b"unexpected",
        )
    assert caught.value.code == "color_not_supported"
    assert calls == []

    inferred_id = _supplier(csv_column_mapping={
        "title": {"column": "name", "type": "string"},
        "colors": {"column": "shade", "type": "list"},
    })
    assert not supports_direct_color(inferred_id)


def test_invalid_parser_config_is_rejected_before_fetch():
    calls = []
    invalid = _supplier(csv_delimiter=";;")
    assert not supports_direct_color(invalid)
    with pytest.raises(SupplierColorFeedError) as caught:
        read_supplier_color_observations(
            invalid,
            fetch_bytes=lambda url: calls.append(url) or b"unexpected",
        )
    assert caught.value.code == "invalid_config"
    assert calls == []


def test_invalid_direct_color_list_is_not_returned_as_an_observation():
    body = _csv([
        ["source_id", "name", "shade", "forged_version", "forged_fields"],
        [
            "unsafe", "Товар с control", "красный|плох\x01цвет", "1",
            '{"colors":["fake"]}',
        ],
    ])
    result = read_supplier_color_observations(_supplier(), fetch_bytes=lambda _url: body)
    assert result.total_rows == 1
    assert result.colors_by_external_id == {}


def test_rejects_fetch_errors_without_echoing_transport_details():
    with pytest.raises(SupplierColorFeedError) as caught:
        read_supplier_color_observations(
            _supplier(),
            fetch_bytes=lambda _url: (_ for _ in ()).throw(RuntimeError("secret payload")),
        )
    assert caught.value.code == "fetch_failed"
    assert "secret payload" not in str(caught.value)


def test_supports_only_legacy_sexoptovik_without_mapping_and_reads_direct_cells():
    row = [""] * 18
    row[0] = "123"
    row[2] = "Legacy товар"
    row[9] = "красный, синий"
    supplier = _supplier(
        code="sexoptovik",
        csv_has_header=False,
        csv_column_mapping=None,
    )

    assert supports_direct_color(supplier)
    result = read_supplier_color_observations(
        supplier,
        fetch_bytes=lambda _url: _csv([row]),
    )
    assert result.total_rows == 1
    assert result.colors_by_external_id == {"123": ("красный", "синий")}
