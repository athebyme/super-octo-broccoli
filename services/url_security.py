# -*- coding: utf-8 -*-
"""
URL security helpers — защита от SSRF, open-redirect и NaN-injection.

Используется в маршрутах, которые принимают URL от пользователя
(parser-источники, фото-прокси, импорт по ссылке) или редиректят
по непроверенным значениям. Также содержит safe_float для защиты
от float("nan") / float("inf") в form-инпутах.
"""
import ipaddress
import math
import socket
from urllib.parse import urlparse


def validate_external_url(url: str) -> str | None:
    """
    Проверяет URL для исходящего HTTP-запроса (SSRF protection).

    Возвращает строку с ошибкой если URL небезопасен (приватный IP,
    нестандартная схема), либо None если проверка пройдена.

    Делает DNS resolve hostname'а и проверяет ВСЕ полученные адреса —
    защита от DNS rebinding.
    """
    if not url or not isinstance(url, str):
        return 'URL пуст или имеет некорректный тип'

    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return 'URL должен начинаться с http:// или https://'

    hostname = parsed.hostname
    if not hostname:
        return 'URL не содержит hostname'

    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return f'Не удалось разрешить hostname: {hostname}'

    for _family, _type, _proto, _canonname, sockaddr in resolved:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return f'Не удалось распарсить IP: {sockaddr[0]}'
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return 'URL не должен указывать на внутренние/приватные адреса'

    return None


def is_safe_local_path(url: str) -> bool:
    """
    Проверяет, что URL — безопасный относительный путь внутри приложения
    (для open-redirect protection в next/return URL'ах).

    Допускает только пути, которые:
      - начинаются с одного слеша (но не двух — `//evil.com`),
      - не содержат scheme и netloc.
    """
    if not url or not isinstance(url, str):
        return False
    if not url.startswith('/'):
        return False
    # `//evil.com/path` — protocol-relative URL: парсер вытащит netloc
    if url.startswith('//'):
        return False
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return False
    return True


def safe_float(value, default: float | None = None) -> float | None:
    """
    Безопасное преобразование form-значения в float.

    Защищает от NaN-injection: строки "nan", "inf", "-inf" парсятся
    стандартным float() без ошибки и потом ломают сравнения
    (NaN > X всегда False, что обходит проверки порогов).

    Возвращает default, если значение пусто / не парсится / не finite.
    """
    if value is None or value == '':
        return default
    try:
        result = float(value)
    except (ValueError, TypeError):
        return default
    if not math.isfinite(result):
        return default
    return result
