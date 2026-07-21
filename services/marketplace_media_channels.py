# -*- coding: utf-8 -*-
"""ORM-free provider boundary for ordered marketplace galleries.

Tenant authorization and target grounding stay in
``marketplace_media_publications``.  Adapters receive only exact external
identities and in-memory credentials.  WB is implemented now; Ozon exposes the
same constraints/typed target contract but deliberately has no direct write
path until it is connected to the existing full-state publication lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlparse

from services.marketplace_adapters.types import MarketplaceCredentials
from services.wb_api_client import WildberriesAPIClient


WB_MAX_IMAGES = 30
WB_ALLOWED_MEDIA_HOST_SUFFIXES = (
    '.wbbasket.ru',
    '.wildberries.ru',
    '.wb.ru',
)


class MarketplaceMediaChannelError(RuntimeError):
    """A provider gallery cannot be read or changed safely."""

    def __init__(self, message: str, *, code: str = 'media_channel_error'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MediaChannelConstraints:
    marketplace_code: str
    max_images: int
    min_width: int
    min_height: int
    requires_public_https: bool
    publication_supported: bool
    publication_contract: str

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            'marketplace_code': self.marketplace_code,
            'max_images': self.max_images,
            'min_width': self.min_width,
            'min_height': self.min_height,
            'requires_public_https': self.requires_public_https,
            'publication_supported': self.publication_supported,
            'publication_contract': self.publication_contract,
        }


@dataclass(frozen=True)
class WbMediaTarget:
    nm_id: int
    vendor_code: str = ''

    def __post_init__(self) -> None:
        if (
            not isinstance(self.nm_id, int)
            or isinstance(self.nm_id, bool)
            or self.nm_id <= 0
        ):
            raise ValueError('nm_id must be a positive integer')
        clean_vendor = str(self.vendor_code or '').strip()[:100]
        object.__setattr__(self, 'vendor_code', clean_vendor)


@dataclass(frozen=True)
class MediaPhoto:
    source_url: str
    fingerprint_url: str

    def to_state(self) -> Dict[str, str]:
        return {
            'source_url': self.source_url,
            'fingerprint_url': self.fingerprint_url,
        }


@dataclass(frozen=True)
class MediaGallery:
    external_item_id: str
    photos: Tuple[MediaPhoto, ...]
    video_url: Optional[str] = None

    def to_state(self) -> Tuple[Dict[str, str], ...]:
        return tuple(photo.to_state() for photo in self.photos)


def _https_url(value: Any, *, provider_owned: bool) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 2_000:
        return None
    parsed = urlparse(value)
    hostname = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not hostname or parsed.username or parsed.password:
        return None
    if provider_owned and not any(
        hostname == suffix[1:] or hostname.endswith(suffix)
        for suffix in WB_ALLOWED_MEDIA_HOST_SUFFIXES
    ):
        return None
    return value


class WildberriesMediaChannel:
    code = 'wb'
    constraints = MediaChannelConstraints(
        marketplace_code='wb',
        max_images=WB_MAX_IMAGES,
        min_width=700,
        min_height=900,
        requires_public_https=True,
        publication_supported=True,
        publication_contract='content-v3-media-save-single-attempt@2026-07-17',
    )

    def __init__(
        self,
        client_factory: Callable[..., WildberriesAPIClient] = WildberriesAPIClient,
    ) -> None:
        self._client_factory = client_factory

    def _client(self, credentials: MarketplaceCredentials, *, write: bool):
        return self._client_factory(
            api_key=credentials.api_key,
            max_retries=0 if write else 2,
        )

    @staticmethod
    def _gallery(target: WbMediaTarget, card: Mapping[str, Any]) -> MediaGallery:
        raw_nm_id = card.get('nmID')
        if raw_nm_id != target.nm_id:
            raise MarketplaceMediaChannelError(
                'WB вернул карточку с другим nmID', code='wb_target_mismatch',
            )
        raw_photos = card.get('photos')
        if not isinstance(raw_photos, list):
            raise MarketplaceMediaChannelError(
                'WB вернул некорректную галерею', code='wb_gallery_malformed',
            )
        if len(raw_photos) > WB_MAX_IMAGES:
            raise MarketplaceMediaChannelError(
                'Галерея WB превышает поддерживаемый лимит',
                code='wb_gallery_too_large',
            )
        photos = []
        seen = set()
        for index, raw_photo in enumerate(raw_photos):
            if not isinstance(raw_photo, Mapping):
                raise MarketplaceMediaChannelError(
                    f'WB вернул некорректное фото #{index + 1}',
                    code='wb_gallery_malformed',
                )
            source = _https_url(
                raw_photo.get('big') or raw_photo.get('c516x688'),
                provider_owned=True,
            )
            fingerprint = _https_url(
                raw_photo.get('tm')
                or raw_photo.get('c246x328')
                or raw_photo.get('square')
                or source,
                provider_owned=True,
            )
            if not source or not fingerprint:
                raise MarketplaceMediaChannelError(
                    f'WB не вернул прямой HTTPS URL для фото #{index + 1}',
                    code='wb_gallery_url_invalid',
                )
            if source in seen:
                raise MarketplaceMediaChannelError(
                    'WB вернул повторяющиеся фото', code='wb_gallery_duplicate',
                )
            seen.add(source)
            photos.append(MediaPhoto(source, fingerprint))

        video = card.get('video')
        if not video:
            media_files = card.get('mediaFiles')
            if isinstance(media_files, list):
                for item in media_files:
                    if isinstance(item, Mapping) and item.get('mediaType') == 'video':
                        video = item.get('big') or item.get('url') or 'present'
                        break
        return MediaGallery(
            external_item_id=str(target.nm_id),
            photos=tuple(photos),
            video_url=str(video)[:2_000] if video else None,
        )

    def read_galleries(
        self,
        credentials: MarketplaceCredentials,
        targets: Iterable[WbMediaTarget],
        *,
        audit_seller_id: Optional[int] = None,
    ) -> Dict[int, MediaGallery]:
        exact_targets = tuple(targets)
        if not exact_targets:
            return {}
        if len(exact_targets) > 200:
            raise ValueError('At most 200 WB media targets are allowed')
        by_nm_id = {target.nm_id: target for target in exact_targets}
        if len(by_nm_id) != len(exact_targets):
            raise ValueError('WB media targets must be unique')
        cards = self._client(credentials, write=False).fetch_cards_by_nm_ids(
            list(by_nm_id),
            log_to_db=bool(audit_seller_id),
            seller_id=audit_seller_id,
        )
        if not isinstance(cards, dict):
            raise MarketplaceMediaChannelError(
                'WB вернул некорректный ответ списка карточек',
                code='wb_cards_malformed',
            )
        unknown = set(cards) - set(by_nm_id)
        if unknown:
            raise MarketplaceMediaChannelError(
                'WB вернул карточки вне запрошенного набора',
                code='wb_cards_exact_set_mismatch',
            )
        result = {}
        for nm_id, card in cards.items():
            if not isinstance(card, Mapping):
                raise MarketplaceMediaChannelError(
                    'WB вернул некорректную карточку', code='wb_card_malformed',
                )
            result[nm_id] = self._gallery(by_nm_id[nm_id], card)
        return result

    def read_gallery(
        self,
        credentials: MarketplaceCredentials,
        target: WbMediaTarget,
        *,
        audit_seller_id: Optional[int] = None,
    ) -> MediaGallery:
        galleries = self.read_galleries(
            credentials, (target,), audit_seller_id=audit_seller_id,
        )
        gallery = galleries.get(target.nm_id)
        if gallery is None:
            raise MarketplaceMediaChannelError(
                'Карточка не найдена в кабинете WB', code='wb_card_not_found',
            )
        return gallery

    def submit_gallery_once(
        self,
        credentials: MarketplaceCredentials,
        target: WbMediaTarget,
        public_urls: Iterable[str],
        *,
        audit_seller_id: Optional[int] = None,
    ) -> Mapping[str, Any]:
        urls = tuple(public_urls)
        if not 1 <= len(urls) <= WB_MAX_IMAGES:
            raise ValueError('WB gallery must contain between 1 and 30 images')
        if len(set(urls)) != len(urls):
            raise ValueError('WB gallery URLs must be unique')
        for url in urls:
            if _https_url(url, provider_owned=False) != url:
                raise ValueError('WB media/save requires direct HTTPS URLs')
        return self._client(credentials, write=True).upload_photos_by_url(
            target.nm_id,
            list(urls),
            seller_id=audit_seller_id,
        )


class OzonMediaChannel:
    code = 'ozon'
    constraints = MediaChannelConstraints(
        marketplace_code='ozon',
        max_images=30,
        min_width=200,
        min_height=200,
        requires_public_https=True,
        publication_supported=False,
        publication_contract='full-state-product-publication-required',
    )

    @staticmethod
    def submit_gallery_once(*_args, **_kwargs):
        raise MarketplaceMediaChannelError(
            'Медиа Ozon будет публиковаться только через full-state operation',
            code='ozon_media_full_state_required',
        )


class MarketplaceMediaChannelRegistry:
    """Explicit registry; adding a provider is a reviewed code change."""

    def __init__(self, channels: Iterable[Any] = ()) -> None:
        self._channels = {}
        for channel in channels:
            code = str(getattr(channel, 'code', '')).strip().lower()
            if not code or code in self._channels:
                raise ValueError('Marketplace media channel code must be unique')
            self._channels[code] = channel

    def get(self, code: str):
        normalized = str(code or '').strip().lower()
        channel = self._channels.get(normalized)
        if channel is None:
            raise MarketplaceMediaChannelError(
                'Маркетплейс не поддерживается media-контуром',
                code='media_marketplace_unsupported',
            )
        return channel

    def manifests(self) -> Dict[str, Dict[str, Any]]:
        return {
            code: self._channels[code].constraints.to_public_dict()
            for code in sorted(self._channels)
        }


_registry: Optional[MarketplaceMediaChannelRegistry] = None


def get_media_channel_registry() -> MarketplaceMediaChannelRegistry:
    global _registry
    if _registry is None:
        _registry = MarketplaceMediaChannelRegistry((
            WildberriesMediaChannel(),
            OzonMediaChannel(),
        ))
    return _registry
