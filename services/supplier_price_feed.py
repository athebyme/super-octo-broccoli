"""Bounded, public-only reader for supplier price CSV feeds.

The feed reader deliberately has no ORM, supplier credentials, logging, retry,
redirect, or cache behavior. Callers own persistence and scheduling.
"""

from __future__ import annotations

import codecs
import csv
import http.client
import ipaddress
import io
import math
import queue
import re
import socket
import ssl
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

MAX_URL_LENGTH = 4096
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_ROWS = 60_000
MAX_COLUMNS = 16
MAX_FIELD_CHARS = 4096
MAX_HEADER_BYTES = 16 * 1024
MAX_HEADER_COUNT = 64
MAX_HEADER_LINE_BYTES = 4096
TOTAL_DEADLINE_SECONDS = 20.0
DNS_CONCURRENCY = 2
MAX_RESPONSE_BYTES = MAX_BODY_BYTES + MAX_HEADER_BYTES

_MAX_SOURCE_ID = (1 << 63) - 1
_MAX_QUANTITY = (1 << 31) - 1
_MAX_PRICE_TEXT = 64
_MAX_VENDOR_TEXT = 200
_MAX_STATUS_TEXT = 100
_MAX_BARCODE_TEXT = 200
_NUMBER_RE = re.compile(r"^(?:[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+)$", re.ASCII)
_POSITIVE_INT_RE = re.compile(r"^[0-9]+$", re.ASCII)
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z", re.ASCII)
_HEADER_ID_NAMES = frozenset({
    "id", "productid", "supplierid", "ид", "идтовара", "idтовара",
    "код", "кодтовара", "кодпозиции", "товарныйкод", "productcode",
    "артикул", "артикултовара", "номертовара",
})
_ALLOWED_CONTENT_TYPES = frozenset({
    "text/csv", "text/plain", "application/csv", "application/octet-stream",
    "application/vnd.ms-excel",
})
_DNS_SLOTS = threading.BoundedSemaphore(DNS_CONCURRENCY)
_ERROR_CODES = frozenset("""
    invalid_url insecure_url url_not_public unsupported_origin dns_failed
    non_public_address resolver_busy network_error deadline_exceeded
    redirect_not_allowed http_status headers_invalid headers_too_large
    body_too_large response_too_large content_type_not_allowed invalid_encoding
    invalid_delimiter invalid_csv invalid_row_shape invalid_source_id
    duplicate_source_id invalid_price invalid_quantity field_too_long empty_feed
    too_many_rows
""".split())


class SupplierPriceFeedError(ValueError):
    """A feed failure carrying only a short, non-sensitive enum code."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else "network_error"
        super().__init__(f"Supplier price feed error: {self.code}")


@dataclass(frozen=True)
class SupplierPriceRow:
    vendor_code: Optional[str]
    price: Optional[float]
    quantity: Optional[int]
    status: Optional[str]
    additional_vendor_code: Optional[str]
    barcode: Optional[str]
    rrp: Optional[float]


def _fail(code: str) -> None:
    raise SupplierPriceFeedError(code) from None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _fail("deadline_exceeded")
    return remaining


def _safe_host(host: str) -> str:
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, AttributeError):
        _fail("invalid_url")
    if (
        not ascii_host
        or len(ascii_host) > 253
        or "." not in ascii_host
        or ascii_host.endswith(".")
        or any(
            len(label) > 63 or not label or not _HOST_LABEL_RE.fullmatch(label)
            for label in ascii_host.split(".")
        )
    ):
        _fail("invalid_url")
    try:
        ipaddress.ip_address(ascii_host)
    except ValueError:
        return ascii_host
    _fail("invalid_url")


def _prepare_url(url: str) -> tuple[str, str]:
    """Return a canonical host/path, upgrading only the documented legacy URL."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        _fail("invalid_url")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        raw_host = parts.hostname
        port = parts.port
    except (TypeError, ValueError):
        _fail("invalid_url")

    if not raw_host:
        _fail("invalid_url")
    if parts.username is not None or parts.password is not None:
        _fail("url_not_public")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        _fail("url_not_public")
    if parts.netloc.endswith(":"):
        _fail("invalid_url")

    if scheme == "http":
        if (
            raw_host.lower() != "sexoptovik.ru"
            or port not in (None, 80)
            or parts.netloc.lower() not in {"sexoptovik.ru", "sexoptovik.ru:80"}
        ):
            _fail("insecure_url")
        raw_host = "sexoptovik.ru"
    elif scheme != "https":
        _fail("insecure_url")
    elif port not in (None, 443):
        _fail("unsupported_origin")

    host = _safe_host(raw_host)
    path = parts.path or "/"
    if (
        not path.startswith("/")
        or path.startswith("//")
        or len(path) > MAX_URL_LENGTH
        or "\\" in path
        or any(ord(char) <= 0x20 or ord(char) == 0x7F or ord(char) > 0x7E for char in path)
        or re.search(r"%(?![0-9A-Fa-f]{2})", path)
    ):
        _fail("invalid_url")
    return host, path


def _resolve_addresses(host: str, deadline: float) -> list[str]:
    """Resolve within the operation deadline and reject mixed/private answers."""
    if not _DNS_SLOTS.acquire(blocking=False):
        _fail("resolver_busy")
    results: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            try:
                answer = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            except Exception:
                answer = None
            results.put_nowait(answer)
        finally:
            _DNS_SLOTS.release()

    worker = threading.Thread(target=resolve, name="supplier-feed-dns", daemon=True)
    try:
        worker.start()
    except Exception:
        _DNS_SLOTS.release()
        _fail("dns_failed")
    try:
        answer = results.get(timeout=_remaining(deadline))
    except queue.Empty:
        _fail("deadline_exceeded")
    if not answer:
        _fail("dns_failed")

    addresses: list[str] = []
    for entry in answer:
        try:
            family, socktype, _proto, _canonname, sockaddr = entry
            if socktype not in (0, socket.SOCK_STREAM):
                _fail("dns_failed")
            if family not in (socket.AF_INET, socket.AF_INET6):
                _fail("dns_failed")
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, TypeError, ValueError):
            _fail("dns_failed")
        if not address.is_global:
            _fail("non_public_address")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        _fail("dns_failed")
    return addresses


def _set_socket_timeout(sock: socket.socket, deadline: float) -> None:
    sock.settimeout(_remaining(deadline))


class _DeadlineRawSocketReader(io.RawIOBase):
    """Socket reader that applies the shrinking wall deadline on every recv."""

    def __init__(self, sock: ssl.SSLSocket, deadline: float):
        super().__init__()
        self._sock = sock
        self._deadline = deadline
        self._response_bytes = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        _set_socket_timeout(self._sock, self._deadline)
        try:
            count = self._sock.recv_into(buffer)
        except (OSError, ssl.SSLError, TimeoutError):
            _remaining(self._deadline)
            _fail("network_error")
        if not count:
            return 0
        self._response_bytes += count
        if self._response_bytes > MAX_RESPONSE_BYTES:
            _fail("response_too_large")
        return count


class _DeadlineSocketAdapter:
    """Minimal socket interface expected by http.client.HTTPResponse."""

    def __init__(self, sock: ssl.SSLSocket, deadline: float):
        self._sock = sock
        self._deadline = deadline

    def makefile(self, mode: str):
        if mode != "rb":
            raise ValueError("read-only response stream required")
        return io.BufferedReader(_DeadlineRawSocketReader(self._sock, self._deadline))


def _validated_headers(response: http.client.HTTPResponse) -> dict[str, str]:
    values: dict[str, list[str]] = {}
    if len(response.reason or "") + 15 > MAX_HEADER_LINE_BYTES:
        _fail("headers_too_large")
    pairs = response.getheaders()
    if len(pairs) > MAX_HEADER_COUNT:
        _fail("headers_too_large")
    if any(
        len(name) + len(value) + 4 > MAX_HEADER_LINE_BYTES
        for name, value in pairs
    ):
        _fail("headers_too_large")
    total = sum(len(name) + len(value) + 4 for name, value in pairs)
    if total > MAX_HEADER_BYTES:
        _fail("headers_too_large")
    for name, value in pairs:
        values.setdefault(name.lower(), []).append(value)
    special = {"content-length", "transfer-encoding", "content-type", "content-encoding"}
    if any(len(values.get(name, [])) > 1 for name in special):
        _fail("headers_invalid")
    return {name: items[0] for name, items in values.items() if items}


def _fetch_over_pinned_https(
    host: str, path: str, addresses: list[str], deadline: float,
) -> bytes:
    """Issue exactly one credential-free HTTPS GET using the first checked IP."""
    if not addresses:
        _fail("dns_failed")
    address = addresses[0]
    raw_sock = None
    tls_sock = None
    response = None
    try:
        parsed_ip = ipaddress.ip_address(address)
        family = socket.AF_INET6 if parsed_ip.version == 6 else socket.AF_INET
        raw_sock = socket.socket(family, socket.SOCK_STREAM)
        _set_socket_timeout(raw_sock, deadline)
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        raw_sock.connect(sockaddr)
        _set_socket_timeout(raw_sock, deadline)
        context = ssl.create_default_context()
        tls_sock = context.wrap_socket(raw_sock, server_hostname=host)
        _set_socket_timeout(tls_sock, deadline)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: SellerHubSupplierFeed/1.0\r\n"
            "Accept: text/csv, application/csv, application/octet-stream;q=0.8\r\n"
            "Accept-Encoding: identity\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        tls_sock.sendall(request)

        response = http.client.HTTPResponse(
            _DeadlineSocketAdapter(tls_sock, deadline), method="GET",
        )
        response.begin()
        if 300 <= response.status < 400:
            _fail("redirect_not_allowed")
        if response.status != 200:
            _fail("http_status")
        headers = _validated_headers(response)

        content_encoding = headers.get("content-encoding", "identity").lower().strip()
        if content_encoding not in ("", "identity"):
            _fail("headers_invalid")
        content_type = headers.get("content-type")
        if content_type:
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type not in _ALLOWED_CONTENT_TYPES:
                _fail("content_type_not_allowed")

        content_length = headers.get("content-length")
        transfer_encoding = headers.get("transfer-encoding")
        if content_length is not None and transfer_encoding is not None:
            _fail("headers_invalid")
        if content_length is not None:
            if not _POSITIVE_INT_RE.fullmatch(content_length):
                _fail("headers_invalid")
            if int(content_length) > MAX_BODY_BYTES:
                _fail("body_too_large")
        if transfer_encoding is not None and transfer_encoding.lower().strip() != "chunked":
            _fail("headers_invalid")

        body = bytearray()
        while True:
            _remaining(deadline)
            amount = min(64 * 1024, MAX_BODY_BYTES + 1 - len(body))
            chunk = response.read(amount)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                _fail("body_too_large")
        _remaining(deadline)
        return bytes(body)
    except SupplierPriceFeedError:
        raise
    except http.client.IncompleteRead:
        _remaining(deadline)
        _fail("network_error")
    except http.client.HTTPException:
        _remaining(deadline)
        _fail("headers_invalid")
    except (OSError, ssl.SSLError, TimeoutError, ValueError):
        _remaining(deadline)
        _fail("network_error")
    finally:
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        sock_to_close = tls_sock or raw_sock
        if sock_to_close is not None:
            try:
                sock_to_close.close()
            except OSError:
                pass


def _text_value(value: str, maximum: int) -> Optional[str]:
    if len(value) > maximum:
        _fail("field_too_long")
    value = value.strip()
    if not value:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _fail("invalid_csv")
    return value


def _normalize_header_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lstrip("\ufeff").casefold()
    return re.sub(r"[\s_\-]+", "", normalized)


def _is_textual_header(row: list[str]) -> bool:
    return bool(row) and _normalize_header_label(row[0]) in _HEADER_ID_NAMES


def _parse_source_id(value: str) -> int:
    raw = value.strip().lstrip("\ufeff")
    if not _POSITIVE_INT_RE.fullmatch(raw):
        _fail("invalid_source_id")
    try:
        parsed = int(raw)
    except ValueError:
        _fail("invalid_source_id")
    if parsed <= 0 or parsed > _MAX_SOURCE_ID:
        _fail("invalid_source_id")
    return parsed


def _parse_optional_price(value: str, *, code: str) -> Optional[float]:
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > _MAX_PRICE_TEXT or not _NUMBER_RE.fullmatch(raw):
        _fail(code)
    try:
        parsed = float(raw.replace(",", "."))
    except (OverflowError, ValueError):
        _fail(code)
    if not math.isfinite(parsed) or parsed < 0:
        _fail(code)
    return parsed


def _parse_optional_quantity(value: str) -> Optional[int]:
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > 10 or not _POSITIVE_INT_RE.fullmatch(raw):
        _fail("invalid_quantity")
    try:
        parsed = int(raw)
    except ValueError:
        _fail("invalid_quantity")
    if parsed < 0 or parsed > _MAX_QUANTITY:
        _fail("invalid_quantity")
    return parsed


def _parse_csv(
    body: bytes, *, encoding: str, delimiter: str,
    deadline: Optional[float] = None,
) -> dict[int, SupplierPriceRow]:
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in ("\r", "\n", '"'):
        _fail("invalid_delimiter")
    if not isinstance(encoding, str) or not encoding or len(encoding) > 40:
        _fail("invalid_encoding")
    try:
        codec = codecs.lookup(encoding)
        text = body.decode(codec.name, errors="strict")
    except (LookupError, UnicodeDecodeError):
        _fail("invalid_encoding")
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        rows: dict[int, SupplierPriceRow] = {}
        header_checked = False
        data_rows = 0
        for fields in reader:
            if deadline is not None and data_rows % 1000 == 0:
                _remaining(deadline)
            if not fields or all(not field.strip() for field in fields):
                continue
            if not header_checked:
                header_checked = True
                if _is_textual_header(fields):
                    continue
            if len(fields) < 4 or len(fields) > MAX_COLUMNS:
                _fail("invalid_row_shape")
            if any(len(field) > MAX_FIELD_CHARS for field in fields):
                _fail("field_too_long")
            data_rows += 1
            if data_rows > MAX_ROWS:
                _fail("too_many_rows")
            source_id = _parse_source_id(fields[0])
            if source_id in rows:
                _fail("duplicate_source_id")
            vendor_code = _text_value(fields[1], _MAX_VENDOR_TEXT)
            price = _parse_optional_price(fields[2], code="invalid_price")
            quantity = _parse_optional_quantity(fields[3])
            status = _text_value(fields[4], _MAX_STATUS_TEXT) if len(fields) > 4 else None
            additional_vendor_code = (
                _text_value(fields[5], _MAX_VENDOR_TEXT) if len(fields) > 5 else None
            )
            barcode = _text_value(fields[6], _MAX_BARCODE_TEXT) if len(fields) > 6 else None
            rrp = (
                _parse_optional_price(fields[7], code="invalid_price")
                if len(fields) > 7 else None
            )
            # The current feed includes a ninth column unused by the legacy
            # mapping; later columns are accepted within the same size cap.
            rows[source_id] = SupplierPriceRow(
                vendor_code=vendor_code,
                price=price,
                quantity=quantity,
                status=status,
                additional_vendor_code=additional_vendor_code,
                barcode=barcode,
                rrp=rrp,
            )
    except SupplierPriceFeedError:
        raise
    except (csv.Error, UnicodeError, ValueError):
        _fail("invalid_csv")
    if not rows:
        _fail("empty_feed")
    if deadline is not None:
        _remaining(deadline)
    return rows


def read_supplier_price_feed(
    url: str,
    *,
    encoding: str,
    delimiter: str,
) -> dict[int, SupplierPriceRow]:
    """Fetch and parse one bounded public supplier price CSV.

    The function performs at most one GET. It never sends cookies, auth
    headers, URL userinfo, or query parameters, and never exposes transport or
    payload details in exceptions.
    """
    deadline = time.monotonic() + TOTAL_DEADLINE_SECONDS
    host, path = _prepare_url(url)
    addresses = _resolve_addresses(host, deadline)
    body = _fetch_over_pinned_https(host, path, addresses, deadline)
    return _parse_csv(body, encoding=encoding, delimiter=delimiter, deadline=deadline)


__all__ = [
    "SupplierPriceFeedError",
    "SupplierPriceRow",
    "read_supplier_price_feed",
]
