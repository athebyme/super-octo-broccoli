# -*- coding: utf-8 -*-
"""Списочный read-model «Моих товаров» для интерфейса на Vue.

Экран массовых операций бесполезен, если по строке нельзя решить, стоит ли её
выбирать. Поэтому лента отдаёт не только карточку импорта, но и три вещи,
которые раньше приходилось искать на других страницах:

* сигнал качества (`quality_score`, причины «требует внимания»);
* спрос за 30 дней (просмотры, заказы, выкуп) — продаётся товар или завис;
* фактические цена и остаток на канале рядом с закупочными.

Сервис только читает: никаких записей и вызовов маркетплейсов. Все запросы
seller-scoped, объём страницы ограничен, каналы догружаются пакетно, чтобы не
получить N+1 на 200 строках.
"""

import unicodedata
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import load_only

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceProductDraft,
    Product,
    SellerMarketplaceAccount,
    SupplierProduct,
    db,
)

MAX_PAGE_SIZE = 200
CATALOG_CATEGORY_PAGE_SIZE = 100
_MAX_CATALOG_CATEGORY_PAGE = ((1 << 63) - 1) // CATALOG_CATEGORY_PAGE_SIZE
_CATALOG_CATEGORY_CONTROL_GLOB = (
    '*[' + chr(1) + '-' + chr(31) + chr(127) + '-' + chr(159) + ']*'
)
# imported_pending_sync — карточка создана на WB, но артикул ещё не получен
STATUSES = frozenset({
    'pending', 'validated', 'imported', 'imported_pending_sync', 'failed',
})
SORTS = frozenset({'newest', 'oldest', 'price_asc', 'price_desc', 'title'})
MARKETPLACES = frozenset({'wb', 'ozon'})
MARKETPLACE_STATES = frozenset({'active', 'preparation', 'attention'})
_WB_WORKFLOW_STATUSES = ('validated', 'imported_pending_sync', 'failed')

# Причины, по которым карточку стоит трогать в первую очередь.
# Совпадают с кодами card_quality_scorer.ATTENTION_REASONS.
_REASON_LIMIT = 4


class MyProductsFeedError(ValueError):
    """Некорректный запрос ленты."""


class MyProductsFeedNotFound(MyProductsFeedError):
    """One or more exact seller-owned rows are no longer available."""


class MyProductsFeedFeatureDisabled(MyProductsFeedError):
    """A requested marketplace view is disabled for this deployment."""


def _positive_int(value: Any, name: str, default: Optional[int] = None) -> int:
    if value in (None, ''):
        if default is None:
            raise MyProductsFeedError(f'{name} обязателен')
        return default
    if isinstance(value, bool):
        raise MyProductsFeedError(f'{name} должен быть целым числом')
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise MyProductsFeedError(f'{name} должен быть целым числом') from None
    if parsed <= 0:
        raise MyProductsFeedError(f'{name} должен быть положительным')
    return parsed


def _json_list(raw: Any) -> list:
    import json

    try:
        value = json.loads(raw or '')
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _photo_count(product: ImportedProduct) -> int:
    return len(_json_list(product.photo_urls))


def _first_photo(product: ImportedProduct, wb_product: Optional[Product]) -> Optional[str]:
    # Фото поставщика отдаём через собственный прокси: прямые CDN-ссылки
    # поставщика требуют его cookies и из браузера продавца не открываются.
    if _json_list(product.photo_urls):
        return f'/api/photos/imported-product/{product.id}/0'
    # Опубликованная карточка: разворачиваем слоты галереи WB
    if wb_product is not None and wb_product.nm_id:
        slots = _json_list(wb_product.photos_json)
        if slots:
            from services.wb_media import normalize_photo_urls

            urls = normalize_photo_urls(wb_product.nm_id, slots[:1], 'big')
            for value in urls:
                if isinstance(value, str) and value.startswith('http'):
                    return value
    return None


class MyProductsFeed:
    """Read-only лента карточек продавца с контекстом для решения."""

    @classmethod
    def list_products(
        cls,
        *,
        seller_id: int,
        status: Optional[str] = None,
        search: Optional[str] = None,
        supplier: Optional[str] = None,
        brand: Optional[str] = None,
        wb_category: Optional[str] = None,
        catalog_category: Optional[str] = None,
        has_photos: Optional[str] = None,
        stock: Optional[str] = None,
        updates_only: bool = False,
        marketplace: Optional[str] = None,
        marketplace_state: Optional[str] = None,
        ozon_enabled: bool = False,
        sort: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        seller_id = _positive_int(seller_id, 'seller_id')
        page = _positive_int(page, 'page', 1)
        per_page = _positive_int(per_page, 'per_page', 50)
        if per_page > MAX_PAGE_SIZE:
            raise MyProductsFeedError(
                f'per_page не может быть больше {MAX_PAGE_SIZE}'
            )
        if status and status not in STATUSES:
            raise MyProductsFeedError('Неизвестный статус')
        if sort and sort not in SORTS:
            raise MyProductsFeedError('Неизвестная сортировка')
        marketplace = cls._filter_value(
            marketplace, 'marketplace', MARKETPLACES,
        )
        marketplace_state = cls._filter_value(
            marketplace_state, 'marketplace_state', MARKETPLACE_STATES,
        )
        if marketplace == 'ozon' and not ozon_enabled:
            raise MyProductsFeedFeatureDisabled('Фильтр Ozon отключён')
        catalog_category_kind, catalog_category_label = (
            cls._parse_catalog_category_filter(catalog_category)
        )

        query = ImportedProduct.query.filter_by(seller_id=seller_id)
        if marketplace and not marketplace_state:
            query = query.filter(cls._marketplace_association(
                seller_id=seller_id,
                marketplace=marketplace,
            ))
        if marketplace_state:
            if marketplace == 'wb':
                state_filter = cls._wb_state(
                    seller_id=seller_id,
                    state=marketplace_state,
                )
            elif marketplace == 'ozon':
                state_filter = cls._ozon_state(
                    seller_id=seller_id,
                    state=marketplace_state,
                )
            else:
                state_filters = [cls._wb_state(
                    seller_id=seller_id,
                    state=marketplace_state,
                )]
                if ozon_enabled:
                    state_filters.append(cls._ozon_state(
                        seller_id=seller_id,
                        state=marketplace_state,
                    ))
                state_filter = db.or_(*state_filters)
            query = query.filter(state_filter)
        if status:
            query = query.filter_by(import_status=status)
        if updates_only:
            query = query.join(
                SupplierProduct,
                SupplierProduct.id == ImportedProduct.supplier_product_id,
            ).filter(
                SupplierProduct.content_revision
                > ImportedProduct.supplier_content_revision,
            )
        if search:
            term = str(search).strip()[:200]
            if term:
                # SQLite не сворачивает регистр кириллицы — перебираем варианты
                variants = []
                for candidate in (
                    term, term.lower(), term.upper(),
                    term.capitalize(), term.title(),
                ):
                    if candidate and candidate not in variants:
                        variants.append(candidate)
                conditions = []
                for variant in variants:
                    pattern = f'%{variant}%'
                    conditions.extend((
                        ImportedProduct.title.ilike(pattern),
                        ImportedProduct.brand.ilike(pattern),
                        ImportedProduct.external_id.ilike(pattern),
                        ImportedProduct.external_vendor_code.ilike(pattern),
                    ))
                query = query.filter(db.or_(*conditions))
        if supplier == 'none':
            query = query.filter(ImportedProduct.supplier_id.is_(None))
        elif supplier:
            query = query.filter(
                ImportedProduct.supplier_id == _positive_int(supplier, 'supplier')
            )
        if brand == 'none':
            query = query.filter(db.or_(
                ImportedProduct.brand.is_(None),
                ImportedProduct.brand == '',
            ))
        elif brand:
            query = query.filter(ImportedProduct.brand == brand)
        if wb_category == 'none':
            query = query.filter(db.or_(
                ImportedProduct.mapped_wb_category.is_(None),
                ImportedProduct.mapped_wb_category == '',
            ))
        elif wb_category:
            query = query.filter(
                ImportedProduct.mapped_wb_category == wb_category
            )
        if catalog_category_kind == 'missing':
            query = query.filter(cls._catalog_category_expression().is_(None))
        elif catalog_category_kind == 'label':
            query = query.filter(
                cls._catalog_category_expression() == catalog_category_label
            )
        if has_photos == 'yes':
            query = query.filter(
                ImportedProduct.photo_urls.isnot(None),
                ImportedProduct.photo_urls != '',
                ImportedProduct.photo_urls != '[]',
            )
        elif has_photos == 'no':
            query = query.filter(db.or_(
                ImportedProduct.photo_urls.is_(None),
                ImportedProduct.photo_urls == '',
                ImportedProduct.photo_urls == '[]',
            ))
        if stock == 'in_stock':
            query = query.filter(ImportedProduct.supplier_quantity > 0)
        elif stock == 'out_of_stock':
            query = query.filter(ImportedProduct.supplier_quantity == 0)

        sort_map = {
            'oldest': ImportedProduct.created_at.asc(),
            'price_asc': ImportedProduct.supplier_price.asc(),
            'price_desc': ImportedProduct.supplier_price.desc(),
            'title': ImportedProduct.title.asc(),
        }
        query = query.order_by(
            sort_map.get(sort, ImportedProduct.created_at.desc()),
            ImportedProduct.id.desc(),
        )
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False,
        )
        rows = pagination.items
        context = cls._page_context(seller_id=seller_id, rows=rows)
        return {
            'items': [cls._serialize(row, context) for row in rows],
            'pagination': {
                'page': pagination.page,
                'per_page': pagination.per_page,
                'pages': pagination.pages,
                'total': pagination.total,
                'has_next': pagination.has_next,
            },
        }

    @staticmethod
    def _filter_value(
        value: Optional[str],
        name: str,
        allowed: frozenset,
    ) -> Optional[str]:
        if value in (None, ''):
            return None
        if not isinstance(value, str) or value not in allowed:
            message = {
                'marketplace': 'Неизвестная площадка',
                'marketplace_state': 'Неизвестный статус карточек',
            }.get(name, 'Неизвестный фильтр')
            raise MyProductsFeedError(message)
        return value

    @staticmethod
    def _catalog_category_expression():
        """The exact category label shown by the catalog row presenter."""
        return db.func.coalesce(
            db.func.nullif(ImportedProduct.mapped_wb_category, ''),
            db.func.nullif(ImportedProduct.category, ''),
        )

    @staticmethod
    def _parse_catalog_category_filter(value: Optional[str]):
        """Parse the explicit missing/label token used by the beta catalog."""
        if value in (None, ''):
            return None, None
        if not isinstance(value, str):
            raise MyProductsFeedError('Некорректная категория каталога')
        if value == 'missing':
            return 'missing', None
        prefix = 'label:'
        if not value.startswith(prefix):
            raise MyProductsFeedError('Некорректная категория каталога')
        label = value[len(prefix):]
        if not MyProductsFeed._catalog_category_label_is_valid(label):
            raise MyProductsFeedError('Некорректная категория каталога')
        return 'label', label

    @staticmethod
    def _catalog_category_label_is_valid(label: Any) -> bool:
        return (
            isinstance(label, str)
            and 1 <= len(label) <= 200
            and not any(
                unicodedata.category(character) == 'Cc'
                for character in label
            )
        )

    @classmethod
    def catalog_category_options(
        cls,
        *,
        seller_id: int,
        page: int = 1,
    ) -> Dict[str, Any]:
        """Return bounded distinct labels for the seller-owned catalog."""
        seller_id = _positive_int(seller_id, 'seller_id')
        page = _positive_int(page, 'page', 1)
        if page > _MAX_CATALOG_CATEGORY_PAGE:
            raise MyProductsFeedError('Страница категорий слишком велика')

        category = cls._catalog_category_expression()
        # SQLite is the production store. Filtering label length and control
        # code points before offset/limit keeps option pages disjoint while the
        # shared Python validator remains the final response boundary.
        labels = db.session.query(category).filter(
            ImportedProduct.seller_id == seller_id,
            category.isnot(None),
            db.func.length(category) <= 200,
            category.op('NOT GLOB')(_CATALOG_CATEGORY_CONTROL_GLOB),
            db.func.instr(category, db.func.char(0)) == 0,
        ).group_by(category).order_by(category.asc()).offset(
            (page - 1) * CATALOG_CATEGORY_PAGE_SIZE
        ).limit(CATALOG_CATEGORY_PAGE_SIZE + 1).all()
        has_next = len(labels) > CATALOG_CATEGORY_PAGE_SIZE
        labels = labels[:CATALOG_CATEGORY_PAGE_SIZE]
        options = [
            {'key': f'label:{label}', 'label': label}
            for (label,) in labels
            if cls._catalog_category_label_is_valid(label)
        ]
        has_uncategorized = ImportedProduct.query.with_entities(
            ImportedProduct.id,
        ).filter(
            ImportedProduct.seller_id == seller_id,
            category.is_(None),
        ).limit(1).first() is not None
        return {
            'options': options,
            'page': page,
            'has_next': has_next,
            'has_uncategorized': has_uncategorized,
        }

    @staticmethod
    def _wb_product_exists(*, seller_id: int, active_only: bool = False):
        """An exact seller-owned WB card with a confirmed positive nmID."""
        product_query = db.session.query(Product.id).filter(
            Product.id == ImportedProduct.product_id,
            Product.seller_id == seller_id,
            Product.nm_id.isnot(None),
            Product.nm_id > 0,
        )
        if active_only:
            product_query = product_query.filter(Product.is_active.is_(True))
        return product_query.exists()

    @classmethod
    def _wb_association(cls, *, seller_id: int):
        return db.or_(
            cls._wb_product_exists(seller_id=seller_id),
            ImportedProduct.import_status.in_(_WB_WORKFLOW_STATUSES),
        )

    @staticmethod
    def _ozon_listing_exists(*, seller_id: int, conditions=()):
        # Build a seller-owned ID set once for the page/count query. A
        # correlated EXISTS here makes SQLite repeat the account and listing
        # lookup for every imported product in a potentially large catalog.
        listing_ids = db.select(MarketplaceListing.imported_product_id).join(
            Marketplace,
            Marketplace.id == MarketplaceListing.marketplace_id,
        ).join(
            SellerMarketplaceAccount,
            SellerMarketplaceAccount.id == MarketplaceListing.account_id,
        ).where(
            MarketplaceListing.imported_product_id.isnot(None),
            MarketplaceListing.seller_id == seller_id,
            Marketplace.code == 'ozon',
            SellerMarketplaceAccount.seller_id == seller_id,
            SellerMarketplaceAccount.marketplace_id
            == MarketplaceListing.marketplace_id,
        )
        listing_ids = listing_ids.where(*conditions)
        return ImportedProduct.id.in_(listing_ids)

    @staticmethod
    def _ozon_draft_exists(*, seller_id: int, statuses=None):
        draft_ids = db.select(
            MarketplaceProductDraft.imported_product_id,
        ).join(
            Marketplace,
            Marketplace.id == MarketplaceProductDraft.marketplace_id,
        ).join(
            SellerMarketplaceAccount,
            SellerMarketplaceAccount.id == MarketplaceProductDraft.account_id,
        ).where(
            MarketplaceProductDraft.imported_product_id.isnot(None),
            MarketplaceProductDraft.seller_id == seller_id,
            MarketplaceProductDraft.published_listing_id.is_(None),
            Marketplace.code == 'ozon',
            SellerMarketplaceAccount.seller_id == seller_id,
            SellerMarketplaceAccount.marketplace_id
            == MarketplaceProductDraft.marketplace_id,
        )
        if statuses is not None:
            draft_ids = draft_ids.where(
                MarketplaceProductDraft.status.in_(statuses),
            )
        return ImportedProduct.id.in_(draft_ids)

    @classmethod
    def _ozon_association(cls, *, seller_id: int):
        # Unknown and archived provider rows remain Ozon associations. The
        # state predicates below decide whether they qualify for a workflow tab.
        return db.or_(
            cls._ozon_listing_exists(seller_id=seller_id),
            cls._ozon_draft_exists(seller_id=seller_id),
        )

    @classmethod
    def _marketplace_association(cls, *, seller_id: int, marketplace: str):
        if marketplace == 'wb':
            return cls._wb_association(seller_id=seller_id)
        return cls._ozon_association(seller_id=seller_id)

    @classmethod
    def _wb_state(cls, *, seller_id: int, state: str):
        if state == 'active':
            return db.and_(
                ImportedProduct.import_status == 'imported',
                cls._wb_product_exists(seller_id=seller_id, active_only=True),
            )
        if state == 'preparation':
            return ImportedProduct.import_status.in_(
                ('validated', 'imported_pending_sync'),
            )
        return ImportedProduct.import_status == 'failed'

    @classmethod
    def _ozon_state(cls, *, seller_id: int, state: str):
        if state == 'active':
            return cls._ozon_listing_exists(
                seller_id=seller_id,
                conditions=(
                    MarketplaceListing.normalized_status == 'active',
                    MarketplaceListing.is_available.is_(True),
                    MarketplaceListing.is_archived.is_(False),
                    SellerMarketplaceAccount.is_active.is_(True),
                ),
            )
        if state == 'preparation':
            return db.or_(
                cls._ozon_listing_exists(
                    seller_id=seller_id,
                    conditions=(
                        MarketplaceListing.normalized_status.in_(
                            ('creating', 'moderation'),
                        ),
                        MarketplaceListing.is_archived.is_(False),
                    ),
                ),
                cls._ozon_draft_exists(
                    seller_id=seller_id,
                    statuses=('needs_category', 'draft', 'ready'),
                ),
            )
        return db.or_(
            cls._ozon_listing_exists(
                seller_id=seller_id,
                conditions=(
                    MarketplaceListing.normalized_status == 'error',
                    MarketplaceListing.is_archived.is_(False),
                ),
            ),
            cls._ozon_draft_exists(
                seller_id=seller_id,
                statuses=('blocked',),
            ),
        )

    @classmethod
    def list_products_by_ids(
        cls,
        *,
        seller_id: int,
        imported_product_ids: List[int],
    ) -> Dict[str, Any]:
        """Serialize one exact seller-owned batch in receipt order."""
        seller_id = _positive_int(seller_id, 'seller_id')
        if (
            not isinstance(imported_product_ids, list)
            or not 1 <= len(imported_product_ids) <= MAX_PAGE_SIZE
        ):
            raise MyProductsFeedError('Набор импорта недействителен')
        seen = set()
        for value in imported_product_ids:
            if (
                type(value) is not int
                or value <= 0
                or value > (1 << 63) - 1
                or value in seen
            ):
                raise MyProductsFeedError('Набор импорта недействителен')
            seen.add(value)

        rows = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.id.in_(imported_product_ids),
        ).all()
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(imported_product_ids):
            raise MyProductsFeedNotFound('Набор импорта недоступен')
        ordered_rows = [by_id[product_id] for product_id in imported_product_ids]
        context = cls._page_context(seller_id=seller_id, rows=ordered_rows)
        count = len(ordered_rows)
        return {
            'items': [cls._serialize(row, context) for row in ordered_rows],
            'pagination': {
                'page': 1,
                'per_page': MAX_PAGE_SIZE,
                'pages': 1,
                'total': count,
                'has_next': False,
            },
            'import_batch_count': count,
        }

    @classmethod
    def facets(cls, *, seller_id: int) -> Dict[str, Any]:
        """Счётчики вкладок одним агрегатом вместо запроса на каждую."""
        seller_id = _positive_int(seller_id, 'seller_id')
        rows = db.session.query(
            ImportedProduct.import_status,
            db.func.count(ImportedProduct.id),
        ).filter_by(seller_id=seller_id).group_by(
            ImportedProduct.import_status
        ).all()
        statuses = {'all': 0}
        for status, count in rows:
            value = int(count or 0)
            statuses[status or 'pending'] = value
            statuses['all'] += value
        updates = db.session.query(
            db.func.count(ImportedProduct.id)
        ).join(
            SupplierProduct,
            SupplierProduct.id == ImportedProduct.supplier_product_id,
        ).filter(
            ImportedProduct.seller_id == seller_id,
            SupplierProduct.content_revision
            > ImportedProduct.supplier_content_revision,
        ).scalar() or 0
        statuses['supplier_updates'] = int(updates)
        return {'statuses': statuses}

    # ------------------------------------------------------------------
    # Контекст страницы: всё, что нужно строкам, одним пакетом запросов
    # ------------------------------------------------------------------

    @classmethod
    def _page_context(
        cls,
        *,
        seller_id: int,
        rows: List[ImportedProduct],
    ) -> Dict[str, Any]:
        product_ids = [row.product_id for row in rows if row.product_id]
        supplier_ids = [
            row.supplier_product_id for row in rows if row.supplier_product_id
        ]
        imported_ids = [row.id for row in rows]

        wb_products: Dict[int, Product] = {}
        if product_ids:
            for product in Product.query.options(load_only(
                Product.id, Product.nm_id, Product.quality_score,
                Product.attention_reasons, Product.quality_impact,
                Product.wb_views_30d, Product.wb_orders_30d,
                Product.wb_price, Product.wb_discounted_price,
                Product.quantity, Product.photos_json, Product.nm_rating,
            )).filter(
                Product.id.in_(product_ids),
                Product.seller_id == seller_id,
            ).all():
                wb_products[product.id] = product

        supplier_updates: Dict[int, bool] = {}
        if supplier_ids:
            for sp_id, revision in db.session.query(
                SupplierProduct.id, SupplierProduct.content_revision,
            ).filter(SupplierProduct.id.in_(supplier_ids)).all():
                supplier_updates[sp_id] = revision

        drafts: Dict[int, List[Dict[str, Any]]] = {}
        listings: Dict[int, List[Dict[str, Any]]] = {}
        if imported_ids:
            # This projection is specifically Ozon.  Match every channel row
            # to an account owned by this seller and to that same marketplace;
            # a malformed foreign or cross-marketplace account link must not
            # leak its label or look like an Ozon listing in this read model.
            draft_rows = db.session.query(
                MarketplaceProductDraft.id,
                MarketplaceProductDraft.imported_product_id,
                MarketplaceProductDraft.account_id,
                MarketplaceProductDraft.status,
                MarketplaceProductDraft.published_listing_id,
                SellerMarketplaceAccount.label,
            ).join(
                Marketplace,
                Marketplace.id == MarketplaceProductDraft.marketplace_id,
            ).join(
                SellerMarketplaceAccount,
                SellerMarketplaceAccount.id == MarketplaceProductDraft.account_id,
            ).filter(
                MarketplaceProductDraft.seller_id == seller_id,
                MarketplaceProductDraft.imported_product_id.in_(imported_ids),
                MarketplaceProductDraft.marketplace_id
                == SellerMarketplaceAccount.marketplace_id,
                Marketplace.code == 'ozon',
                SellerMarketplaceAccount.seller_id == seller_id,
            ).all()
            for (
                draft_id, imported_id, account_id, status, published_id,
                account_label,
            ) in draft_rows:
                drafts.setdefault(imported_id, []).append({
                    'id': draft_id,
                    'account_id': account_id,
                    'account_label': account_label,
                    'status': status,
                    'published_listing_id': published_id,
                })

            # Keep the query projection-only: MarketplaceListing contains
            # wide JSON snapshots, but exact handoff needs account identity,
            # current availability/archive flags, and normalized status.
            listing_rows = db.session.query(
                MarketplaceListing.id,
                MarketplaceListing.imported_product_id,
                MarketplaceListing.account_id,
                MarketplaceListing.normalized_status,
                Marketplace.code,
                MarketplaceListing.is_available,
                MarketplaceListing.is_archived,
                SellerMarketplaceAccount.label,
                SellerMarketplaceAccount.is_active,
            ).join(
                Marketplace, Marketplace.id == MarketplaceListing.marketplace_id,
            ).join(
                SellerMarketplaceAccount,
                SellerMarketplaceAccount.id == MarketplaceListing.account_id,
            ).filter(
                MarketplaceListing.seller_id == seller_id,
                MarketplaceListing.imported_product_id.in_(imported_ids),
                Marketplace.code == 'ozon',
                SellerMarketplaceAccount.seller_id == seller_id,
                SellerMarketplaceAccount.marketplace_id
                == MarketplaceListing.marketplace_id,
            ).all()
            for (
                listing_id, imported_id, account_id, status, code,
                is_available, is_archived, account_label, account_is_active,
            ) in listing_rows:
                listings.setdefault(imported_id, []).append({
                    'id': listing_id,
                    'account_id': account_id,
                    'account_label': account_label,
                    'status': status,
                    'marketplace_code': code,
                    'is_available': bool(is_available),
                    'is_archived': bool(is_archived),
                    'account_is_active': bool(account_is_active),
                    # The query is grouped by the exact non-null FK to the
                    # current seller-owned ImportedProduct.  Do not expose a
                    # foreign product ID; this typed state is enough for the
                    # caller to require an exact link before handoff.
                    'link_status': 'linked',
                })
        return {
            'wb_products': wb_products,
            'supplier_updates': supplier_updates,
            'drafts': drafts,
            'listings': listings,
        }

    @classmethod
    def _serialize(
        cls,
        row: ImportedProduct,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        wb_product = context['wb_products'].get(row.product_id)
        supplier_revision = context['supplier_updates'].get(
            row.supplier_product_id
        )
        has_update = bool(
            supplier_revision is not None
            and supplier_revision > (row.supplier_content_revision or 0)
        )
        reasons = [
            code for code in
            ((wb_product.attention_reasons or '').split(',') if wb_product else [])
            if code
        ][:_REASON_LIMIT]
        ozon_listings = context['listings'].get(row.id, [])
        ozon_drafts = [
            draft for draft in context['drafts'].get(row.id, [])
            if not draft.get('published_listing_id')
        ]
        return {
            'id': row.id,
            'title': row.title,
            'brand': row.brand,
            'vendor_code': row.external_vendor_code or row.external_id,
            'category': row.mapped_wb_category or row.category,
            'import_status': row.import_status,
            'photo_count': _photo_count(row),
            'photo': _first_photo(row, wb_product),
            'supplier_price': (
                float(row.supplier_price) if row.supplier_price else None
            ),
            'supplier_quantity': row.supplier_quantity,
            'has_supplier_update': has_update,
            'created_at': row.created_at.isoformat() if row.created_at else None,

            # Канал WB: факт публикации и как там идут дела
            'wb': {
                'product_id': row.product_id,
                'nm_id': wb_product.nm_id if wb_product else row.wb_nm_id,
                'price': (
                    float(wb_product.wb_discounted_price or wb_product.wb_price)
                    if wb_product and (wb_product.wb_discounted_price or wb_product.wb_price)
                    else None
                ),
                'quantity': wb_product.quantity if wb_product else None,
                'rating': wb_product.nm_rating if wb_product else None,
                'quality_score': wb_product.quality_score if wb_product else None,
                'quality_impact': wb_product.quality_impact if wb_product else None,
                'attention_reasons': reasons,
                'views_30d': wb_product.wb_views_30d if wb_product else None,
                'orders_30d': wb_product.wb_orders_30d if wb_product else None,
            } if wb_product or row.product_id or row.wb_nm_id else None,

            # Канал Ozon: опубликованные карточки и подготовленные черновики
            'ozon': {
                'listings': ozon_listings,
                'drafts': ozon_drafts,
            } if (ozon_listings or ozon_drafts) else None,
        }
