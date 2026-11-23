"""Bounded, read-only extraction of configured direct supplier color cells."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Optional

from services.supplier_price_feed import MAX_BODY_BYTES, MAX_ROWS
from services.supplier_service import SupplierCSVParser

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_NAME_CHARS = 256
_MAX_SEPARATOR_CHARS = 32
_MAX_FEED_COLUMNS = 4096


class SupplierColorFeedError(ValueError):
    """A safe, stable error code without feed, URL, or supplier data."""

    __slots__ = ("code",)
    _CODES = frozenset({
        "invalid_config", "color_not_supported", "fetch_failed", "invalid_feed",
    })

    def __init__(self, code: str):
        self.code = code if code in self._CODES else "invalid_feed"
        super().__init__(f"Supplier color feed error: {self.code}")


@dataclass(frozen=True)
class SupplierColorFeedConfig:
    """Immutable snapshot of the parser's effective Supplier fields.

    The JSON encoding keeps nested column mappings immutable. The public
    ``csv_column_mapping`` property returns a fresh copy for each parser.
    """

    code: str
    csv_source_url: str
    csv_delimiter: str
    csv_encoding: str
    csv_has_header: bool
    _csv_column_mapping_json: str

    @property
    def csv_column_mapping(self):
        return json.loads(self._csv_column_mapping_json)

    def parser_supplier(self):
        return SimpleNamespace(
            code=self.code,
            csv_source_url=self.csv_source_url,
            csv_delimiter=self.csv_delimiter,
            csv_encoding=self.csv_encoding,
            csv_has_header=self.csv_has_header,
            csv_column_mapping=self.csv_column_mapping,
        )


@dataclass(frozen=True)
class SupplierColorFeed:
    total_rows: int
    colors_by_external_id: dict[str, tuple[str, ...]]
    body_sha256: str


def _freeze_config(supplier) -> SupplierColorFeedConfig:
    try:
        code = getattr(supplier, "code", None)
        source_url = getattr(supplier, "csv_source_url", None)
        delimiter = getattr(supplier, "csv_delimiter", None) or ";"
        encoding = getattr(supplier, "csv_encoding", None) or "cp1251"
        has_header = bool(getattr(supplier, "csv_has_header", False))
        mapping = getattr(supplier, "csv_column_mapping", None)
        if code is None or not isinstance(code, str) or not code or len(code) > _MAX_NAME_CHARS:
            raise ValueError
        if (
            source_url is None
            or not isinstance(source_url, str)
            or not source_url
            or len(source_url) > 4096
        ):
            raise ValueError
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise ValueError
        if not isinstance(encoding, str) or not encoding or len(encoding) > _MAX_NAME_CHARS:
            raise ValueError
        codecs.lookup(encoding)
        if mapping is not None and not isinstance(mapping, dict):
            raise ValueError
        mapping_json = json.dumps(
            mapping,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(mapping_json.encode("utf-8")) > _MAX_CONFIG_BYTES:
            raise ValueError
    except Exception:
        raise SupplierColorFeedError("invalid_config") from None
    return SupplierColorFeedConfig(
        code=code,
        csv_source_url=source_url,
        csv_delimiter=delimiter,
        csv_encoding=encoding,
        csv_has_header=has_header,
        _csv_column_mapping_json=mapping_json,
    )


def _valid_column(value, *, has_header: bool) -> bool:
    if type(value) is int:
        return 0 <= value < 4096
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_NAME_CHARS
        and has_header
    )


def _supports_explicit_identity(config: SupplierColorFeedConfig) -> bool:
    mapping = config.csv_column_mapping
    if not isinstance(mapping, dict) or not mapping:
        return False
    for field_name in ("external_id", "title"):
        field = mapping.get(field_name)
        if not isinstance(field, dict) or field.get("type", "string") != "string":
            return False
        if not _valid_column(field.get("column"), has_header=config.csv_has_header):
            return False
    return True


def _supports_configured_colors(config: SupplierColorFeedConfig) -> bool:
    mapping = config.csv_column_mapping
    if not isinstance(mapping, dict) or not mapping:
        return False
    colors = mapping.get("colors")
    if not isinstance(colors, dict) or colors.get("type") != "list":
        return False
    if not SupplierCSVParser._has_explicit_observed_color_list_role(config.parser_supplier()):
        return False

    columns = colors.get("columns")
    prefix = colors.get("columns_prefix")
    if isinstance(columns, list):
        if not columns or any(
            not _valid_column(column, has_header=config.csv_has_header)
            for column in columns
        ):
            return False
    elif "column" in colors:
        if not _valid_column(colors.get("column"), has_header=config.csv_has_header):
            return False
    elif not (
        isinstance(prefix, str)
        and bool(prefix)
        and len(prefix) <= _MAX_NAME_CHARS
        and config.csv_has_header
    ):
        return False

    separator = colors.get("separator", ",")
    return (
        isinstance(separator, str)
        and bool(separator)
        and len(separator) <= _MAX_SEPARATOR_CHARS
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in separator)
    )


def _supports_direct_color_config(config: SupplierColorFeedConfig) -> bool:
    mapping = config.csv_column_mapping
    if config.code == "sexoptovik" and mapping in (None, {}):
        return True
    return _supports_explicit_identity(config) and _supports_configured_colors(config)


def supports_direct_color(supplier) -> bool:
    """Return whether current parser config has explicit identity and color cells."""
    try:
        config = _freeze_config(supplier)
        return _supports_direct_color_config(config)
    except SupplierColorFeedError:
        return False


def _validate_row_bound(text: str, delimiter: str) -> None:
    try:
        reader = csv.reader(
            io.StringIO(text, newline=""),
            delimiter=delimiter,
            quotechar='"',
            strict=True,
        )
        for row_number, row in enumerate(reader, 1):
            if row_number > MAX_ROWS + 1:  # permit the header in addition to data rows
                raise ValueError
            if len(row) > _MAX_FEED_COLUMNS:
                raise ValueError
    except (csv.Error, UnicodeError, ValueError):
        raise SupplierColorFeedError("invalid_feed") from None


def read_supplier_color_observations(
    supplier,
    *,
    fetch_bytes: Optional[Callable[[str], bytes]] = None,
) -> SupplierColorFeed:
    """Read one exact feed body and return only parser-attested direct colors.

    The default fetch is the existing pinned, public-only, bounded transport.
    Tests and callers may inject a byte reader; this function never persists,
    schedules, enriches, normalizes, retries, or performs a provider request.
    """
    config = _freeze_config(supplier)
    if not _supports_direct_color_config(config):
        raise SupplierColorFeedError("color_not_supported")
    parser_supplier = config.parser_supplier()
    parser = SupplierCSVParser(parser_supplier)

    if fetch_bytes is None:
        from services.supplier_price_feed import read_supplier_csv_bytes

        fetch_bytes = read_supplier_csv_bytes
    try:
        body = fetch_bytes(config.csv_source_url)
    except Exception:
        raise SupplierColorFeedError("fetch_failed") from None
    if type(body) is not bytes or not body or len(body) > MAX_BODY_BYTES:
        raise SupplierColorFeedError("invalid_feed")

    try:
        text = body.decode(config.csv_encoding, errors="strict")
    except (LookupError, UnicodeError):
        raise SupplierColorFeedError("invalid_feed") from None
    _validate_row_bound(text, config.csv_delimiter)

    try:
        expected_ids = parser._catalog_feed_identities(text)
        if not expected_ids or len(expected_ids) > MAX_ROWS:
            raise ValueError
        products = parser.parse(text)
        actual_ids = [product.get("external_id") for product in products]
        if (
            len(products) != len(expected_ids)
            or actual_ids != expected_ids
            or any(
                not isinstance(product.get("title"), str)
                or not product["title"].strip()
                for product in products
            )
        ):
            raise ValueError
    except Exception:
        raise SupplierColorFeedError("invalid_feed") from None

    colors_by_external_id = {}
    for product in products:
        # Parser-owned metadata is never accepted from mapped/raw feed fields.
        direct_product = dict(product)
        SupplierCSVParser._strip_reserved_observation_keys(direct_product)
        observed = SupplierCSVParser._observed_source_fields(direct_product, parser_supplier)
        colors = observed.get("colors")
        if colors is not None:
            colors_by_external_id[product["external_id"]] = tuple(colors)

    return SupplierColorFeed(
        total_rows=len(expected_ids),
        colors_by_external_id=colors_by_external_id,
        body_sha256=hashlib.sha256(body).hexdigest(),
    )
