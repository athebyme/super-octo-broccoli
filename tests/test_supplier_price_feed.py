import socket
import ssl

import pytest

from services import supplier_price_feed as feed


PUBLIC_IP = "93.184.216.34"


def _install_body(monkeypatch, body, *, addresses=None):
    monkeypatch.setattr(
        feed, "_resolve_addresses",
        lambda _host, _deadline: list(addresses or [PUBLIC_IP]),
    )
    monkeypatch.setattr(
        feed, "_fetch_over_pinned_https",
        lambda _host, _path, _addresses, _deadline: body,
    )


def _read(body, *, encoding="utf-8", delimiter=";"):
    return feed._parse_csv(body, encoding=encoding, delimiter=delimiter)


def _error_code(call):
    with pytest.raises(feed.SupplierPriceFeedError) as raised:
        call()
    return raised.value.code, str(raised.value)


def test_parse_accepts_header_blank_lines_zero_values_and_unused_ninth_column():
    body = (
        "\ufeffID;Артикул;Цена;Наличие;Статус;Доп. артикул;Штрихкод;РРЦ;Лишняя колонка\n"
        "\n"
        "101;MAIN-101;0;0;;;;0;ignored value\n"
        "102;MAIN-102;10,5;4;;ALT-102;460000000102;21,75;also ignored\n"
        "103;MAIN-103;;;;;;;\n"
    ).encode("utf-8")

    parsed = _read(body)
    rows = parsed.rows

    assert set(rows) == {101, 102, 103}
    assert parsed.conflicting_ids == frozenset()
    assert rows[101] == feed.SupplierPriceRow(
        vendor_code="MAIN-101", price=0.0, quantity=0, status=None,
        additional_vendor_code=None, barcode=None, rrp=0.0,
    )
    assert rows[102] == feed.SupplierPriceRow(
        vendor_code="MAIN-102", price=10.5, quantity=4, status=None,
        additional_vendor_code="ALT-102", barcode="460000000102", rrp=21.75,
    )
    assert rows[103].price is None
    assert rows[103].quantity is None


def test_read_contract_returns_rows_from_one_mocked_fetch(monkeypatch):
    body = b"id;vendor;price;quantity;status;extra;barcode;rrp;unused\n7;A;3.25;0;;;;;x\n"
    calls = []
    monkeypatch.setattr(feed, "_resolve_addresses", lambda host, deadline: [PUBLIC_IP])

    def fetch(host, path, addresses, deadline):
        calls.append((host, path, addresses))
        return body

    monkeypatch.setattr(feed, "_fetch_over_pinned_https", fetch)
    parsed = feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    )

    assert isinstance(parsed, feed.SupplierPriceFeed)
    assert parsed.conflicting_ids == frozenset()
    rows = parsed.rows
    assert rows[7].price == 3.25
    assert rows[7].quantity == 0
    assert calls == [("feed.example", "/prices.csv", [PUBLIC_IP])]


def test_parser_accepts_headerless_rows_and_optional_short_rows():
    rows = _read(b"7;A;3;1\n8;B;;;\n").rows
    assert rows[7].status is None
    assert rows[8].price is None
    assert rows[8].quantity is None


@pytest.mark.parametrize(
    ("csv_row", "expected_code"),
    [
        ("0;A;1;1", "invalid_source_id"),
        ("x;A;1;1", "invalid_source_id"),
        ("9223372036854775808;A;1;1", "invalid_source_id"),
        ("7;A;NaN;1", "invalid_price"),
        ("7;A;-1;1", "invalid_price"),
        ("7;A;inf;1", "invalid_price"),
        ("7;A;1,2,3;1", "invalid_price"),
        ("7;A;1;-1", "invalid_quantity"),
        ("7;A;1;2147483648", "invalid_quantity"),
        ("7;A;1", "invalid_row_shape"),
    ],
)
def test_invalid_required_values_fail_closed_with_short_code(csv_row, expected_code):
    code, message = _error_code(lambda: _read((csv_row + "\n").encode()))
    assert code == expected_code
    assert "\n" not in message
    assert csv_row not in message


def test_identical_duplicate_numeric_source_ids_coalesce_across_aliases():
    parsed = _read(b"7;A;1.00;1\n007; A ;1,0;01;;;;\n")

    assert parsed.rows == {
        7: feed.SupplierPriceRow(
            vendor_code="A", price=1.0, quantity=1, status=None,
            additional_vendor_code=None, barcode=None, rrp=None,
        ),
    }
    assert parsed.conflicting_ids == frozenset()


def test_conflicting_duplicate_ids_are_quarantined_without_losing_safe_rows():
    parsed = _read(
        b"7;A;1;1\n8;SAFE;5;0\n007;A;2;1\n9;OTHER;3;2\n"
    )

    assert set(parsed.rows) == {8, 9}
    assert parsed.conflicting_ids == frozenset({7})


def test_later_repeat_cannot_restore_a_quarantined_source_id():
    parsed = _read(b"7;A;1;1\n007;A;2;1\n7;A;1;1\n8;SAFE;3;0\n")

    assert set(parsed.rows) == {8}
    assert parsed.conflicting_ids == frozenset({7})


def test_malformed_later_repeat_still_fails_the_entire_feed():
    code, _message = _error_code(
        lambda: _read(b"7;A;1;1\n007;A;2;1\n7;A;NaN;1\n8;SAFE;3;0\n")
    )
    assert code == "invalid_price"


def test_feed_with_only_conflicting_ids_is_rejected_as_nonusable():
    code, _message = _error_code(lambda: _read(b"7;A;1;1\n007;A;2;1\n"))
    assert code == "empty_feed"


def test_non_public_or_mixed_dns_answers_fail_before_transport(monkeypatch):
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443)),
    ]
    calls = []
    monkeypatch.setattr(
        feed.socket, "getaddrinfo", lambda _host, _port, type: answers,
    )

    def should_not_fetch(*_args):
        calls.append(True)
        return b"7;A;1;1\n"

    monkeypatch.setattr(feed, "_fetch_over_pinned_https", should_not_fetch)
    code, _message = _error_code(lambda: feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    ))
    assert code == "non_public_address"
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/prices.csv",
        "http://www.sexoptovik.ru/prices.csv",
        "http://sexoptovik.ru:81/prices.csv",
        "http://sexoptovik.ru/prices.csv?token=secret",
        "http://user:pass@sexoptovik.ru/prices.csv",
        "https://user:pass@feed.example/prices.csv",
        "https://feed.example/prices.csv?token=secret",
        "https://127.0.0.1/prices.csv",
        "https://feed.example:444/prices.csv",
    ],
)
def test_unsafe_urls_are_rejected_without_echoing_url(url):
    code, message = _error_code(lambda: feed._prepare_url(url))
    assert code in {"insecure_url", "unsupported_origin", "url_not_public", "invalid_url"}
    assert url not in message
    assert "secret" not in message


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://sexoptovik.ru/catalog/price.csv", ("sexoptovik.ru", "/catalog/price.csv")),
        ("http://sexoptovik.ru:80/catalog/price.csv", ("sexoptovik.ru", "/catalog/price.csv")),
        ("https://sexoptovik.ru/catalog/price.csv", ("sexoptovik.ru", "/catalog/price.csv")),
    ],
)
def test_only_exact_documented_legacy_origin_is_upgraded(url, expected):
    assert feed._prepare_url(url) == expected


class _FakeRawSocket:
    def __init__(self, _family, _socktype):
        self.connected_to = None
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def connect(self, address):
        self.connected_to = address

    def close(self):
        self.closed = True


class _FakeTLSSocket:
    def __init__(self, response, *, fail_after_send=False):
        self.response = response
        self.fail_after_send = fail_after_send
        self.request = None
        self.timeout = None
        self.closed = False
        self.offset = 0

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, value):
        self.request = value
        if self.fail_after_send:
            raise OSError("simulated uncertain send")

    def recv_into(self, buffer):
        available = len(self.response) - self.offset
        if available <= 0:
            return 0
        count = min(len(buffer), available)
        memoryview(buffer)[:count] = self.response[self.offset:self.offset + count]
        self.offset += count
        return count

    def close(self):
        self.closed = True


class _FakeTLSContext:
    def __init__(self, tls_socket):
        self.tls_socket = tls_socket
        self.server_hostname = None

    def wrap_socket(self, _raw_socket, *, server_hostname):
        self.server_hostname = server_hostname
        return self.tls_socket


def _install_fake_transport(
    monkeypatch, response, dns_entries=None, *, fail_after_send=False,
):
    raw_sockets = []
    tls_sockets = []
    contexts = []

    def make_raw(family, socktype):
        raw = _FakeRawSocket(family, socktype)
        raw_sockets.append(raw)
        return raw

    def make_context():
        tls = _FakeTLSSocket(response, fail_after_send=fail_after_send)
        context = _FakeTLSContext(tls)
        tls_sockets.append(tls)
        contexts.append(context)
        return context

    monkeypatch.setattr(feed.socket, "socket", make_raw)
    monkeypatch.setattr(feed.ssl, "create_default_context", make_context)
    monkeypatch.setattr(
        feed.socket, "getaddrinfo",
        lambda _host, _port, type: dns_entries or [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443)),
        ],
    )
    return raw_sockets, tls_sockets, contexts


def test_mocked_https_transport_pins_ip_uses_hostname_and_sends_no_credentials(monkeypatch):
    body = b"11;A;9;0;;;;;x\n"
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/csv; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    raw_sockets, tls_sockets, contexts = _install_fake_transport(monkeypatch, response)

    parsed = feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    )

    assert parsed.rows[11].quantity == 0
    assert raw_sockets[0].connected_to == (PUBLIC_IP, 443)
    assert contexts[0].server_hostname == "feed.example"
    request = tls_sockets[0].request
    assert request.startswith(b"GET /prices.csv HTTP/1.1\r\n")
    assert b"Host: feed.example\r\n" in request
    assert b"Authorization:" not in request
    assert b"Cookie:" not in request
    assert b"secret" not in request
    assert tls_sockets[0].closed


def test_read_supplier_csv_bytes_returns_raw_headerless_18_column_body(monkeypatch):
    body = b";".join(f"field-{index}".encode() for index in range(18)) + b"\n"
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/csv; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    raw_sockets, tls_sockets, contexts = _install_fake_transport(monkeypatch, response)

    result = feed.read_supplier_csv_bytes("https://feed.example/catalog.csv")

    assert result == body
    assert raw_sockets[0].connected_to == (PUBLIC_IP, 443)
    assert contexts[0].server_hostname == "feed.example"
    assert tls_sockets[0].request.startswith(b"GET /catalog.csv HTTP/1.1\r\n")
    assert len(tls_sockets) == 1
    assert tls_sockets[0].closed


def test_redirect_is_rejected_and_never_followed(monkeypatch):
    response = (
        b"HTTP/1.1 302 Found\r\n"
        b"Location: https://elsewhere.example/private.csv\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    _raw, tls, _contexts = _install_fake_transport(monkeypatch, response)
    code, message = _error_code(lambda: feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    ))
    assert code == "redirect_not_allowed"
    assert "elsewhere" not in message
    assert len(tls) == 1


def test_http_error_is_not_retried(monkeypatch):
    response = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
    _raw, tls, _contexts = _install_fake_transport(monkeypatch, response)
    code, message = _error_code(lambda: feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    ))
    assert code == "http_status"
    assert "503" not in message
    assert len(tls) == 1


def test_truncated_content_length_is_not_returned_as_a_valid_prefix(monkeypatch):
    valid_prefix = b"7;A;3;1\n"
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/csv\r\n"
        b"Content-Length: 100\r\n\r\n"
        + valid_prefix
    )
    _install_fake_transport(monkeypatch, response)
    code, message = _error_code(lambda: feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    ))
    assert code == "network_error"
    assert "7;A" not in message


def test_uncertain_send_does_not_try_a_second_resolved_address(monkeypatch):
    dns_entries = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ]
    raw, tls, _contexts = _install_fake_transport(
        monkeypatch, b"", dns_entries=dns_entries, fail_after_send=True,
    )
    code, _message = _error_code(lambda: feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    ))
    assert code == "network_error"
    assert len(raw) == 1
    assert len(tls) == 1
    assert tls[0].request is not None


def test_socket_reader_recomputes_remaining_timeout_for_each_receive(monkeypatch):
    class TickingSocket:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, value):
            self.timeouts.append(value)

        def recv_into(self, buffer):
            memoryview(buffer)[:1] = b"x"
            return 1

    clock_values = iter((10.0, 10.25))
    monkeypatch.setattr(feed.time, "monotonic", lambda: next(clock_values))
    sock = TickingSocket()
    reader = feed._DeadlineRawSocketReader(sock, deadline=11.0)

    assert reader.readinto(bytearray(1)) == 1
    assert reader.readinto(bytearray(1)) == 1
    assert sock.timeouts == [1.0, 0.75]


def test_chunked_response_is_bounded_and_parsed(monkeypatch):
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/csv\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"8\r\n7;A;3;0\n\r\n"
        b"0\r\n\r\n"
    )
    _install_fake_transport(monkeypatch, response)
    parsed = feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    )
    assert parsed.rows[7].price == 3.0
    assert parsed.rows[7].quantity == 0


def test_content_length_and_header_bounds_fail_closed(monkeypatch):
    oversized = (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(feed.MAX_BODY_BYTES + 1).encode()
        + b"\r\n\r\n"
    )
    _install_fake_transport(monkeypatch, oversized)
    code, _message = _error_code(lambda: feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    ))
    assert code == "body_too_large"

    too_long_header = (
        b"HTTP/1.1 200 OK\r\nX-Large: "
        + b"a" * (feed.MAX_HEADER_LINE_BYTES + 1)
        + b"\r\n\r\n"
    )
    _install_fake_transport(monkeypatch, too_long_header)
    code, _message = _error_code(lambda: feed.read_supplier_price_feed(
        "https://feed.example/prices.csv", encoding="utf-8", delimiter=";",
    ))
    assert code == "headers_too_large"
