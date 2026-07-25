"""Immutable public image assets for asynchronous marketplace imports.

Supplier image URLs are observed source facts, but they are not necessarily
directly fetchable by a marketplace crawler.  Some suppliers return a small
HTML cookie/meta-refresh challenge before the real image.  This module
downloads such sources server-side through the existing SSRF-safe Image Lab
transport, verifies and normalizes the bytes, and stores an immutable JPEG
addressed by its SHA-256 digest.

The public route can resolve only a signed digest that already exists in the
local asset store.  It is therefore not an open image proxy and never performs
network I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from io import BytesIO
import ipaddress
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlencode, urlsplit
import warnings

from PIL import Image, ImageOps
import requests

from services.image_lab_service import ImageLabError, download_public_image


ASSET_CONTRACT_VERSION = 1
ASSET_ROUTE_PREFIX = "/marketplace-assets/images/"
ASSET_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ASSET_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
MIN_IMAGE_SIDE = 300
DEFAULT_URLS_PER_ATTEMPT = 3
DEFAULT_ATTEMPT_SECONDS = 40


class MarketplaceImageAssetError(ValueError):
    """Safe, bounded media preparation error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "marketplace_image_asset_error",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class MarketplaceImagePreparationResult:
    payload: dict
    complete: bool
    prepared_now: int
    prepared_total: int
    source_total: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False


def _positive_config_integer(
    config: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _asset_root(config: Mapping[str, Any]) -> Path:
    raw = str(
        config.get("MARKETPLACE_IMAGE_ASSET_DIR")
        or os.environ.get("MARKETPLACE_IMAGE_ASSET_DIR")
        or "data/marketplace_image_assets"
    ).strip()
    if not raw:
        raise MarketplaceImageAssetError(
            "Не настроено хранилище фотографий маркетплейсов",
            code="media_asset_storage_unavailable",
        )
    return Path(raw).expanduser().resolve()


def _public_base(config: Mapping[str, Any]) -> str:
    raw = str(config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        raise MarketplaceImageAssetError(
            "Не задан публичный HTTPS-адрес Seller Hub для передачи фото Ozon",
            code="media_public_base_url_missing",
        )
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").strip().lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise MarketplaceImageAssetError(
            "Публичный адрес Seller Hub для фото должен быть HTTPS",
            code="media_public_base_url_invalid",
        )
    if hostname == "localhost" or hostname.endswith(".local"):
        raise MarketplaceImageAssetError(
            "Ozon не сможет скачать фото с локального адреса Seller Hub",
            code="media_public_base_url_not_public",
        )
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise MarketplaceImageAssetError(
            "Публичный адрес Seller Hub для фото недоступен из интернета",
            code="media_public_base_url_not_public",
        )
    return raw


def _signature(secret_key: str, digest: str) -> str:
    message = f"marketplace-image:v{ASSET_CONTRACT_VERSION}:{digest}"
    return hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _asset_path(config: Mapping[str, Any], digest: str) -> Path:
    if not ASSET_DIGEST_PATTERN.fullmatch(digest):
        raise MarketplaceImageAssetError(
            "Некорректный идентификатор фото",
            code="media_asset_not_found",
        )
    root = _asset_root(config)
    path = root / digest[:2] / f"{digest}.jpg"
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise MarketplaceImageAssetError(
            "Некорректный путь фото",
            code="media_asset_not_found",
        ) from None
    return path


def _verified_asset_path(
    config: Mapping[str, Any],
    digest: str,
) -> Path:
    path = _asset_path(config, digest)
    try:
        if (
            not path.is_file()
            or not 512 <= path.stat().st_size <= MAX_OUTPUT_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise OSError("asset integrity mismatch")
    except OSError:
        raise MarketplaceImageAssetError(
            "Подготовленное фото Seller Hub больше недоступно",
            code="media_asset_missing",
        ) from None
    return path


def public_asset_url(
    *,
    config: Mapping[str, Any],
    secret_key: str,
    digest: str,
) -> str:
    base = _public_base(config)
    signature = _signature(secret_key, digest)
    return (
        f"{base}{ASSET_ROUTE_PREFIX}{digest}.jpg?"
        + urlencode({"sig": signature})
    )


def _asset_identity_from_url(
    value: str,
    *,
    config: Mapping[str, Any],
    secret_key: str,
) -> Optional[str]:
    """Return a verified local digest, None for an ordinary external URL."""
    if not isinstance(value, str) or not value:
        return None
    base = _public_base(config)
    base_parsed = urlsplit(base)
    parsed = urlsplit(value)
    expected_prefix = (
        base_parsed.path.rstrip("/") + ASSET_ROUTE_PREFIX
    )
    if (
        parsed.scheme != base_parsed.scheme
        or parsed.netloc != base_parsed.netloc
        or not parsed.path.startswith(expected_prefix)
    ):
        return None
    suffix = parsed.path[len(expected_prefix):]
    if not suffix.endswith(".jpg"):
        raise MarketplaceImageAssetError(
            "Подписанная ссылка Seller Hub на фото повреждена",
            code="media_asset_url_invalid",
        )
    digest = suffix[:-4]
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.fragment
        or set(query) != {"sig"}
        or len(query["sig"]) != 1
        or not ASSET_SIGNATURE_PATTERN.fullmatch(query["sig"][0])
        or not ASSET_DIGEST_PATTERN.fullmatch(digest)
        or not hmac.compare_digest(
            query["sig"][0],
            _signature(secret_key, digest),
        )
    ):
        raise MarketplaceImageAssetError(
            "Подписанная ссылка Seller Hub на фото повреждена",
            code="media_asset_url_invalid",
        )
    _verified_asset_path(config, digest)
    return digest


def _normalized_jpeg(data: bytes) -> bytes:
    if not isinstance(data, bytes) or not data:
        raise MarketplaceImageAssetError(
            "Источник фото вернул пустой файл",
            code="media_source_invalid_image",
        )
    if len(data) > MAX_SOURCE_BYTES:
        raise MarketplaceImageAssetError(
            "Исходное фото больше 20 МБ",
            code="media_source_too_large",
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            source = Image.open(BytesIO(data))
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise MarketplaceImageAssetError(
                    "Разрешение исходного фото превышает safety limit",
                    code="media_source_resolution_too_large",
                )
            source.load()
            image = ImageOps.exif_transpose(source)
            width, height = image.size
            if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
                raise MarketplaceImageAssetError(
                    f"Фото меньше {MIN_IMAGE_SIDE}×{MIN_IMAGE_SIDE} пикселей",
                    code="media_source_resolution_too_small",
                )
            if image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            rendered = None
            for quality in (94, 90, 86, 82, 78, 74):
                output = BytesIO()
                image.save(
                    output,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
                candidate = output.getvalue()
                if len(candidate) <= MAX_OUTPUT_BYTES:
                    rendered = candidate
                    break
            if rendered is None:
                raise MarketplaceImageAssetError(
                    "Подготовленное JPEG-фото больше 12 МБ",
                    code="media_asset_too_large",
                )
            return rendered
    except MarketplaceImageAssetError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MarketplaceImageAssetError(
            "Разрешение исходного фото превышает safety limit",
            code="media_source_resolution_too_large",
        ) from None
    except Exception:
        raise MarketplaceImageAssetError(
            "Источник вернул невалидное изображение",
            code="media_source_invalid_image",
        ) from None


def _store_jpeg(
    data: bytes,
    *,
    config: Mapping[str, Any],
) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = _asset_path(config, digest)
    if path.is_file():
        try:
            if (
                path.stat().st_size == len(data)
                and hashlib.sha256(path.read_bytes()).hexdigest() == digest
            ):
                return digest
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{digest}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError:
        raise MarketplaceImageAssetError(
            "Не удалось сохранить подготовленное фото",
            code="media_asset_storage_unavailable",
            retryable=True,
        ) from None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return digest


def _download_error(error: Exception, label: str) -> MarketplaceImageAssetError:
    message = str(error)
    lowered = message.casefold()
    if (
        "локальн" in lowered
        or "некорректный url" in lowered
        or "разрешить адрес" in lowered
    ):
        return MarketplaceImageAssetError(
            f"{label}: источник фото запрещён политикой безопасности",
            code="media_source_url_forbidden",
        )
    if (
        "html без безопасного redirect" in lowered
        or "не декодируется" in lowered
        or "слишком маленькое" in lowered
        or "пуст" in lowered
        or "http 404" in lowered
        or "http 400" in lowered
        or "http 403" in lowered
    ):
        return MarketplaceImageAssetError(
            f"{label}: источник не отдал корректное изображение",
            code="media_source_invalid_image",
        )
    retryable = isinstance(
        error,
        (
            requests.Timeout,
            requests.ConnectionError,
        ),
    ) or any(
        token in lowered
        for token in (
            "timeout",
            "timed out",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "время подготовки",
        )
    )
    return MarketplaceImageAssetError(
        (
            f"{label}: источник фото временно недоступен"
            if retryable
            else f"{label}: не удалось безопасно получить изображение"
        ),
        code=(
            "media_source_temporarily_unavailable"
            if retryable
            else "media_source_invalid_image"
        ),
        retryable=retryable,
    )


def _payload_slots(item: dict) -> list[tuple[str, Optional[int], str]]:
    slots: list[tuple[str, Optional[int], str]] = []
    primary = item.get("primary_image")
    if isinstance(primary, str) and primary:
        slots.append(("primary_image", None, "Главное фото"))
    images = item.get("images")
    if isinstance(images, list):
        slots.extend(
            ("images", index, f"Фото {index + 1}")
            for index, value in enumerate(images)
            if isinstance(value, str) and value
        )
    color = item.get("color_image")
    if isinstance(color, str) and color:
        slots.append(("color_image", None, "Цветовой образец"))
    return slots


def _slot_key(field: str, index: Optional[int]) -> str:
    return f"{field}:{index}" if index is not None else field


def _slot_value(item: dict, field: str, index: Optional[int]) -> str:
    if field == "images":
        return item[field][index]
    return item[field]


def _set_slot(
    item: dict,
    field: str,
    index: Optional[int],
    value: str,
) -> None:
    if field == "images":
        item[field][index] = value
    else:
        item[field] = value


def _deduplicate_item_media(item: dict) -> None:
    primary = item.get("primary_image")
    seen = {primary} if isinstance(primary, str) and primary else set()
    images = item.get("images")
    if isinstance(images, list):
        unique = []
        for value in images:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)
        item["images"] = unique
    color = item.get("color_image")
    if isinstance(color, str) and color in seen:
        item.pop("color_image", None)


def materialize_product_payload(
    payload: dict,
    *,
    config: Mapping[str, Any],
    secret_key: str,
    max_urls: Optional[int] = None,
    deadline: Optional[float] = None,
    selected_slots: Optional[list[str]] = None,
) -> MarketplaceImagePreparationResult:
    """Materialize a bounded prefix of one exact product-import payload."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"items"}
        or not isinstance(payload.get("items"), list)
        or len(payload["items"]) != 1
        or not isinstance(payload["items"][0], dict)
    ):
        raise MarketplaceImageAssetError(
            "Снимок карточки имеет неизвестный media-формат",
            code="media_payload_invalid",
        )
    if not isinstance(secret_key, str) or not secret_key:
        raise MarketplaceImageAssetError(
            "Не настроена подпись публичных фото Seller Hub",
            code="media_asset_signing_unavailable",
        )
    _public_base(config)
    limit = max_urls or _positive_config_integer(
        config,
        "OZON_MEDIA_ASSET_URLS_PER_ATTEMPT",
        DEFAULT_URLS_PER_ATTEMPT,
        minimum=1,
        maximum=10,
    )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        raise MarketplaceImageAssetError(
            "Некорректный лимит подготовки фото",
            code="media_asset_configuration_invalid",
        )
    if deadline is None:
        seconds = _positive_config_integer(
            config,
            "OZON_MEDIA_ASSET_ATTEMPT_SECONDS",
            DEFAULT_ATTEMPT_SECONDS,
            minimum=10,
            maximum=55,
        )
        deadline = time.monotonic() + seconds

    result = {
        "items": [
            dict(payload["items"][0])
        ],
    }
    original_images = payload["items"][0].get("images")
    if isinstance(original_images, list):
        result["items"][0]["images"] = list(original_images)
    item = result["items"][0]
    all_slots = _payload_slots(item)
    if not all_slots:
        raise MarketplaceImageAssetError(
            "В карточке нет фотографий для подготовки",
            code="media_images_required",
        )
    if selected_slots is None:
        slots = all_slots
    else:
        if (
            not isinstance(selected_slots, list)
            or len(selected_slots) > 31
            or any(
                not isinstance(value, str) or not value
                for value in selected_slots
            )
            or len(selected_slots) != len(set(selected_slots))
        ):
            raise MarketplaceImageAssetError(
                "Список source media slots повреждён",
                code="media_payload_invalid",
            )
        by_key = {
            _slot_key(field, index): (field, index, label)
            for field, index, label in all_slots
        }
        if any(value not in by_key for value in selected_slots):
            raise MarketplaceImageAssetError(
                "Source media slot исчез из снимка операции",
                code="media_payload_invalid",
            )
        slots = [by_key[value] for value in selected_slots]

    prepared_now = 0
    prepared_total = 0
    for field, index, label in slots:
        value = _slot_value(item, field, index)
        try:
            existing_digest = _asset_identity_from_url(
                value,
                config=config,
                secret_key=secret_key,
            )
        except MarketplaceImageAssetError as error:
            return MarketplaceImagePreparationResult(
                payload=result,
                complete=False,
                prepared_now=prepared_now,
                prepared_total=prepared_total,
                source_total=len(slots),
                error_code=error.code,
                error_message=f"{label}: {str(error)}",
                retryable=error.retryable,
            )
        if existing_digest is not None:
            prepared_total += 1
            continue
        if prepared_now >= limit or time.monotonic() >= deadline:
            continue
        try:
            remaining = max(1.0, deadline - time.monotonic())
            read_timeout = min(12.0, remaining)
            raw = download_public_image(
                value,
                max_bytes=MAX_SOURCE_BYTES,
                timeout=(min(4.0, remaining), read_timeout),
                deadline=deadline,
            )
            jpeg = _normalized_jpeg(raw)
            digest = _store_jpeg(jpeg, config=config)
            asset_url = public_asset_url(
                config=config,
                secret_key=secret_key,
                digest=digest,
            )
        except (MarketplaceImageAssetError, ImageLabError, requests.RequestException) as exc:
            error = (
                exc
                if isinstance(exc, MarketplaceImageAssetError)
                else _download_error(exc, label)
            )
            return MarketplaceImagePreparationResult(
                payload=result,
                complete=False,
                prepared_now=prepared_now,
                prepared_total=prepared_total,
                source_total=len(slots),
                error_code=error.code,
                error_message=(
                    str(error)
                    if str(error).startswith(label)
                    else f"{label}: {str(error)}"
                ),
                retryable=error.retryable,
            )
        _set_slot(item, field, index, asset_url)
        prepared_now += 1
        prepared_total += 1

    complete = prepared_total == len(slots)
    if complete:
        _deduplicate_item_media(item)
    return MarketplaceImagePreparationResult(
        payload=result,
        complete=complete,
        prepared_now=prepared_now,
        prepared_total=prepared_total,
        source_total=len(slots),
    )


def resolve_public_asset(
    *,
    digest: str,
    signature: str,
    config: Mapping[str, Any],
    secret_key: str,
) -> Path:
    if (
        not ASSET_DIGEST_PATTERN.fullmatch(str(digest or ""))
        or not ASSET_SIGNATURE_PATTERN.fullmatch(str(signature or ""))
        or not hmac.compare_digest(
            str(signature),
            _signature(secret_key, str(digest)),
        )
    ):
        raise MarketplaceImageAssetError(
            "Подпись фото недействительна",
            code="media_asset_signature_invalid",
        )
    try:
        return _verified_asset_path(config, str(digest))
    except MarketplaceImageAssetError:
        raise MarketplaceImageAssetError(
            "Фото не найдено",
            code="media_asset_not_found",
        ) from None
