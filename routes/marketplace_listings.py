"""Seller-facing unified marketplace listing read model."""

import json
import logging
import re
import secrets
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlsplit

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf.csrf import generate_csrf
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from models import MarketplaceCommercialProposal, db
from services.marketplace_accounts import MarketplaceAccountError, MarketplaceAccountService
from services.ozon_account_sync import enqueue_account_sync, latest_account_jobs
from services.marketplace_listings import (
    MarketplaceListingError,
    MarketplaceListingService,
)
from services.marketplace_product_links import (
    MarketplaceProductLinkError,
    MarketplaceProductLinkService,
)
from services.marketplace_canonical_content import (
    MarketplaceCanonicalContentError,
    MarketplaceCanonicalContentNotFound,
    MarketplaceCanonicalContentService,
)
from services.marketplace_warehouses import MarketplaceWarehouseService
from services.marketplace_listing_display import listing_display
from services.marketplace_drafts import (
    MarketplaceDraftError,
    MarketplaceDraftService,
)
from services.ozon_existing_update_batch import OzonExistingUpdateBatch
from services.listing_navigation import (
    build_listing_catalog_url,
    current_listing_catalog_url,
    listing_return_url,
    validate_listing_return_url,
)


marketplace_listings_bp = Blueprint(
    "marketplace_listings",
    __name__,
    url_prefix="/marketplaces/listings",
)

logger = logging.getLogger(__name__)

_EXISTING_UPDATE_TOKEN_SALT = "ozon-existing-update-batch-v1"
_EXISTING_UPDATE_TOKEN_MAX_AGE = 600
_EXISTING_UPDATE_TOKEN_MAX_BYTES = 16_384
_EXISTING_UPDATE_REQUEST_MAX_BYTES = 65_536
_EXISTING_UPDATE_RESPONSE_MAX_BYTES = 524_288
_EXISTING_UPDATE_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_EXISTING_UPDATE_TOKEN_KEYS = {
    "v", "user_id", "seller_id", "account_id", "listing_ids",
    "digest", "return_to", "nonce",
}
_MAX_SIGNED_SQLITE_ID = 9_223_372_036_854_775_807


class _ExistingUpdateReviewError(ValueError):
    status_code = 409
    code = "existing_update_review_invalid"


def _seller_id() -> Optional[int]:
    seller = getattr(current_user, "seller", None)
    return getattr(seller, "id", None) if seller is not None else None


def _wants_json() -> bool:
    return request.is_json or (
        request.accept_mimetypes.best == "application/json"
        and request.accept_mimetypes["application/json"]
        >= request.accept_mimetypes["text/html"]
    )


def _payload() -> Dict[str, Any]:
    if request.is_json:
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}
    return request.form.to_dict(flat=True)


def _integer(value: Any, field_name: str, default: Optional[int] = None) -> int:
    if value in (None, "") and default is not None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} должен быть целым числом")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise ValueError(f"{field_name} должен быть целым числом")
    if parsed <= 0:
        raise ValueError(f"{field_name} должен быть положительным")
    return parsed


def _boolean(value: Any, field_name: str, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "on", "yes"}:
            return True
        if normalized in {"0", "false", "off", "no"}:
            return False
    raise ValueError(f"{field_name} должен быть boolean")


def _payload_integer(data: Dict[str, Any], field_name: str, default: int) -> int:
    value = data.get(field_name)
    if request.is_json and value is not None and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise ValueError(f"{field_name} должен быть целым числом")
    return _integer(value, field_name, default)


def _payload_boolean(data: Dict[str, Any], field_name: str, default: bool) -> bool:
    value = data.get(field_name)
    if request.is_json and value is not None and not isinstance(value, bool):
        raise ValueError(f"{field_name} должен быть boolean")
    return _boolean(value, field_name, default)


def _error_response(error: Exception, status_code: int = 400):
    if isinstance(error, (
        MarketplaceListingError,
        MarketplaceProductLinkError,
        MarketplaceCanonicalContentError,
        MarketplaceAccountError,
        MarketplaceDraftError,
    )):
        status_code = error.status_code
        code = error.code
    else:
        code = "invalid_marketplace_listing_request"
    if _wants_json():
        return jsonify({
            "success": False,
            "error": str(error),
            "code": code,
        }), status_code
    return render_template(
        "marketplace_listing_error.html",
        error=str(error),
    ), status_code


def _catalog_bootstrap_integer(name: str, default: int, maximum: int) -> int:
    """Normalize catalog-only pagination before it reaches Vue/JavaScript."""
    raw = request.args.get(name)
    if not raw or not raw.isascii() or not raw.isdigit():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 1 <= value <= maximum else default


def _listing_filters(*, normalize_pagination: bool = False) -> Dict[str, Any]:
    if normalize_pagination:
        # Keep the initial Vue request aligned with pagination accepted by its
        # JSON API and with page numbers JavaScript can represent precisely.
        page = _catalog_bootstrap_integer(
            "page", 1, 90_071_992_547_409,
        )
        per_page = _catalog_bootstrap_integer("per_page", 60, 100)
    else:
        page = _integer(request.args.get("page"), "page", 1)
        per_page = _integer(request.args.get("per_page"), "per_page", 50)
    return {
        "marketplace_code": request.args.get("marketplace") or None,
        "account_id": (
            _integer(request.args.get("account_id"), "account_id")
            if request.args.get("account_id") else None
        ),
        "normalized_status": request.args.get("status") or None,
        "link_status": request.args.get("link_status") or None,
        "include_unavailable": _boolean(
            request.args.get("include_unavailable"),
            "include_unavailable",
            False,
        ),
        "search": request.args.get("search") or None,
        "page": page,
        "per_page": per_page,
    }


def _catalog_return_url(filters: Dict[str, Any]) -> str:
    try:
        query = request.query_string.decode("ascii")
    except UnicodeDecodeError:
        return build_listing_catalog_url(request.path, filters)
    return current_listing_catalog_url(
        request.path,
        query,
        fallback_filters=filters,
    )


def _detail_return_url(listing=None) -> str:
    marketplace_code = (
        listing.marketplace.code
        if listing is not None and listing.marketplace is not None
        else None
    )


def _existing_update_serializer():
    if not current_app.secret_key:
        raise RuntimeError("Не настроена подпись проверки существующих карточек")
    return URLSafeTimedSerializer(
        current_app.secret_key,
        salt=_EXISTING_UPDATE_TOKEN_SALT,
    )


def _positive_native_id(value: Any, field_name: str) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > _MAX_SIGNED_SQLITE_ID
    ):
        raise ValueError(f"{field_name} должен быть положительным целым числом")
    return value


def _existing_update_ids(value: Any) -> list[int]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= OzonExistingUpdateBatch.MAX_ITEMS
    ):
        raise ValueError(
            "Выберите от 1 до 200 собственных карточек Ozon"
        )
    ids = [_positive_native_id(item, "listing_id") for item in value]
    if len(ids) != len(set(ids)):
        raise ValueError("Выбранные карточки не должны повторяться")
    return ids


def _unique_update_json_pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Поля JSON не должны повторяться")
        data[key] = value
    return data


def _existing_update_body(*, required: set[str], allowed: set[str]) -> dict:
    if request.args:
        raise ValueError("Параметры запроса передаются только в JSON body")
    if not request.is_json:
        raise ValueError("Ожидается JSON body")
    if (
        request.content_length is not None
        and request.content_length > _EXISTING_UPDATE_REQUEST_MAX_BYTES
    ):
        raise ValueError("Запрос превышает допустимый размер")
    body = request.get_data(cache=True)
    if len(body) > _EXISTING_UPDATE_REQUEST_MAX_BYTES:
        raise ValueError("Запрос превышает допустимый размер")
    try:
        data = json.loads(body, object_pairs_hook=_unique_update_json_pairs)
    except (ValueError, UnicodeError, TypeError):
        raise ValueError("Некорректный JSON body") from None
    if not isinstance(data, dict):
        raise ValueError("JSON body должен быть объектом")
    if set(data) - allowed:
        raise ValueError("Переданы неизвестные поля")
    if required - set(data):
        raise ValueError("Не переданы обязательные поля")
    return data


def _default_ozon_catalog_return_url(account_id: int) -> str:
    return build_listing_catalog_url(
        filters={"marketplace": "ozon", "account_id": account_id},
    )


def _validate_ozon_catalog_return_url(value: Any, account_id: int) -> str:
    """Accept only an allowlisted catalog URL pinned to this Ozon account."""
    safe = validate_listing_return_url(value)
    if safe is None:
        raise ValueError("Ссылка возврата в каталог недопустима")
    parsed = urlsplit(safe)
    query = parse_qsl(
        parsed.query,
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=8,
    ) if parsed.query else []
    values = dict(query)
    if (
        values.get("marketplace") != "ozon"
        or values.get("account_id") != str(account_id)
    ):
        raise ValueError("Ссылка возврата должна вести в выбранный кабинет Ozon")
    return safe


def _existing_update_return_url(data: dict, account_id: int) -> str:
    if "return_to" not in data:
        return _default_ozon_catalog_return_url(account_id)
    value = data["return_to"]
    if not isinstance(value, str):
        raise ValueError("return_to должен быть ссылкой на каталог Ozon")
    return _validate_ozon_catalog_return_url(value, account_id)


def _issue_existing_update_token(
    *, user_id: int, seller_id: int, account_id: int,
    listing_ids: list[int], digest: str, return_to: str,
) -> str:
    if not isinstance(digest, str) or not _EXISTING_UPDATE_DIGEST_RE.fullmatch(digest):
        raise MarketplaceDraftError("Сервис вернул некорректную проверку")
    token = _existing_update_serializer().dumps({
        "v": 1,
        "user_id": user_id,
        "seller_id": seller_id,
        "account_id": account_id,
        "listing_ids": listing_ids,
        "digest": digest,
        "return_to": return_to,
        "nonce": secrets.token_urlsafe(18),
    })
    if len(token.encode("utf-8")) > _EXISTING_UPDATE_TOKEN_MAX_BYTES:
        raise MarketplaceDraftError("Проверка выбранных карточек превышает лимит")
    return token


def _load_existing_update_token(
    token: Any,
    *,
    user_id: int,
    seller_id: int,
) -> dict:
    if not isinstance(token, str) or not token:
        raise ValueError("Проверка устарела; перечитайте выбранные карточки")
    try:
        token_size = len(token.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _ExistingUpdateReviewError(
            "Подпись проверки неверна; перечитайте выбранные карточки"
        ) from exc
    if (
        token_size > _EXISTING_UPDATE_TOKEN_MAX_BYTES
    ):
        raise ValueError("Проверка устарела; перечитайте выбранные карточки")
    try:
        payload = _existing_update_serializer().loads(
            token,
            max_age=_EXISTING_UPDATE_TOKEN_MAX_AGE,
        )
    except SignatureExpired as exc:
        raise _ExistingUpdateReviewError(
            "Проверка устарела; перечитайте выбранные карточки"
        ) from exc
    except BadSignature as exc:
        raise _ExistingUpdateReviewError(
            "Подпись проверки неверна; перечитайте выбранные карточки"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _EXISTING_UPDATE_TOKEN_KEYS
        or type(payload.get("v")) is not int
        or payload.get("v") != 1
        or type(payload.get("user_id")) is not int
        or payload.get("user_id") != user_id
        or type(payload.get("seller_id")) is not int
        or payload.get("seller_id") != seller_id
        or type(payload.get("account_id")) is not int
        or not 0 < payload["account_id"] <= _MAX_SIGNED_SQLITE_ID
        or not isinstance(payload.get("listing_ids"), list)
        or not isinstance(payload.get("digest"), str)
        or not _EXISTING_UPDATE_DIGEST_RE.fullmatch(payload["digest"])
        or not isinstance(payload.get("return_to"), str)
        or not isinstance(payload.get("nonce"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{20,40}", payload["nonce"]) is None
    ):
        raise _ExistingUpdateReviewError(
            "Проверка относится к другому продавцу или выбору"
        )
    try:
        listing_ids = _existing_update_ids(payload["listing_ids"])
    except ValueError as exc:
        raise _ExistingUpdateReviewError(
            "Проверка относится к другому набору карточек"
        ) from exc
    if listing_ids != payload["listing_ids"]:
        raise _ExistingUpdateReviewError(
            "Порядок выбранных карточек изменился"
        )
    try:
        safe_return = _validate_ozon_catalog_return_url(
            payload["return_to"], payload["account_id"],
        )
    except ValueError as exc:
        raise _ExistingUpdateReviewError(
            "Проверка содержит недопустимую ссылку возврата"
        ) from exc
    if safe_return != payload["return_to"]:
        raise _ExistingUpdateReviewError(
            "Ссылка возврата проверки изменилась"
        )
    payload["return_to"] = safe_return
    return payload


def _bounded_existing_update_document(
    document: Any,
    *,
    account_id: int,
    listing_ids: list[int],
    allowed_statuses: set[str],
    require_success_draft: bool,
) -> dict:
    if (
        not isinstance(document, dict)
        or type(document.get("account_id")) is not int
        or document.get("account_id") != account_id
        or not isinstance(document.get("listing_ids"), list)
        or any(type(value) is not int for value in document["listing_ids"])
        or document.get("listing_ids") != listing_ids
        or not isinstance(document.get("account_label"), str)
        or len(document["account_label"]) > 500
        or not isinstance(document.get("rows"), list)
        or len(document["rows"]) != len(listing_ids)
    ):
        raise MarketplaceDraftError("Сервис вернул неполный результат проверки")
    rows = []
    success_draft_ids = set()
    for expected_id, source in zip(listing_ids, document["rows"]):
        if (
            not isinstance(source, dict)
            or type(source.get("listing_id")) is not int
            or source.get("listing_id") != expected_id
            or not isinstance(source.get("status"), str)
            or source.get("status") not in allowed_statuses
        ):
            raise MarketplaceDraftError("Порядок или результат карточек изменился")
        row = {"listing_id": expected_id, "status": source["status"]}
        for field in ("title", "offer_id", "message"):
            if field in source:
                value = source[field]
                limit = 1000 if field == "message" else 2000
                if not isinstance(value, str) or len(value) > limit:
                    raise MarketplaceDraftError("Сервис вернул некорректное описание карточки")
                row[field] = value
        if "draft_id" in source:
            row["draft_id"] = _positive_native_id(source["draft_id"], "draft_id")
        if source["status"] == "existing" and "draft_id" not in row:
            raise MarketplaceDraftError("Существующий черновик не подтверждён")
        if source["status"] in {"ready", "blocked"} and "draft_id" in row:
            raise MarketplaceDraftError("Неподготовленная строка не должна содержать черновик")
        if require_success_draft and source["status"] in {"created", "existing"}:
            if "draft_id" not in row or row["draft_id"] in success_draft_ids:
                raise MarketplaceDraftError("Не удалось подтвердить созданный черновик")
            success_draft_ids.add(row["draft_id"])
        elif source["status"] == "blocked" and "draft_id" in row:
            raise MarketplaceDraftError("Заблокированная строка не может открыть черновик")
        rows.append(row)
    result = {
        "account_id": account_id,
        "account_label": document["account_label"],
        "listing_ids": listing_ids,
        "rows": rows,
    }
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _EXISTING_UPDATE_RESPONSE_MAX_BYTES:
        raise MarketplaceDraftError("Результат выбранных карточек превышает лимит")
    return result


def _existing_update_json(payload: dict, status_code: int = 200):
    response = make_response(jsonify(payload), status_code)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _existing_update_error(
    message: str,
    *,
    status_code: int = 400,
    code: str = "invalid_existing_update_request",
):
    return _existing_update_json({
        "success": False,
        "error": message,
        "code": code,
    }, status_code)
    account_id = getattr(listing, "account_id", None) if listing is not None else None
    return listing_return_url(
        request.args.getlist("return_to"),
        marketplace_code=marketplace_code,
        account_id=account_id,
    )


def _redirect_to_detail(listing_id: int, listing=None):
    return redirect(url_for(
        "marketplace_listings.detail",
        listing_id=listing_id,
        return_to=_detail_return_url(listing),
    ))


@marketplace_listings_bp.route("/classic")
@login_required
def classic():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        filters = _listing_filters()
        pagination = MarketplaceListingService.list_listings(
            seller_id=seller_id,
            **filters,
        )
        accounts = MarketplaceAccountService.list_accounts(
            seller_id=seller_id,
        )
        ozon_account_ids = [
            account.id
            for account in accounts
            if account.marketplace and account.marketplace.code == "ozon"
        ]
        latest_syncs = MarketplaceListingService.latest_syncs(
            seller_id=seller_id,
        )
        catalog_completed_at = (
            MarketplaceListingService.latest_completed_catalog_at(
                seller_id=seller_id,
            )
            if ozon_account_ids
            else {}
        )
        onboarding_jobs = latest_account_jobs(
            seller_id=seller_id,
            account_ids=ozon_account_ids,
            accounts=accounts,
        )
    except (MarketplaceListingError, ValueError) as exc:
        return _error_response(exc)
    return render_template(
        "marketplace_listings.html",
        pagination=pagination,
        listings=pagination.items,
        accounts=accounts,
        latest_syncs=latest_syncs,
        catalog_completed_at=catalog_completed_at,
        onboarding_jobs=onboarding_jobs,
        listing_previews={row.id: listing_display(row) for row in pagination.items},
        setup_config={
            'accounts': [account.to_public_dict() for account in accounts if account.marketplace.code == 'ozon'],
            'onboarding_jobs': onboarding_jobs,
            'catalog_syncs': {key: value.to_public_dict() for key, value in latest_syncs.items()},
            'catalog_completed_at': catalog_completed_at,
            'status_url': url_for('marketplace_accounts.list_api'),
            'accounts_url': url_for('marketplace_accounts.index'),
            'ozon_enabled': bool(current_app.config.get('MARKETPLACE_OZON_ENABLED', False)),
        },
        filters=filters,
        catalog_return_url=_catalog_return_url(filters),
        ozon_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ),
    )


@marketplace_listings_bp.route("/")
@marketplace_listings_bp.route("/beta", endpoint="beta")
@login_required
def index():
    """Primary Vue catalogue; /beta remains a compatible alias for saved URLs."""
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        filters = _listing_filters(normalize_pagination=True)
        if filters['account_id'] is not None:
            MarketplaceAccountService.get_owned_account(
                seller_id=seller_id, account_id=filters['account_id'], marketplace_code='ozon',
            )
    except (ValueError, MarketplaceAccountError) as exc:
        return _error_response(exc)
    accounts = MarketplaceAccountService.list_accounts(seller_id=seller_id)
    latest_syncs = MarketplaceListingService.latest_syncs(seller_id=seller_id)
    ozon_account_ids = [
        account.id
        for account in accounts
        if account.marketplace and account.marketplace.code == "ozon"
    ]
    catalog_completed_at = (
        MarketplaceListingService.latest_completed_catalog_at(
            seller_id=seller_id,
        )
        if ozon_account_ids
        else {}
    )
    onboarding_jobs = latest_account_jobs(
        seller_id=seller_id,
        account_ids=ozon_account_ids,
        accounts=accounts,
    )
    accounts_payload = []
    for account in accounts:
        marketplace_code = (
            account.marketplace.code if account.marketplace else None
        )
        if marketplace_code != "ozon":
            continue
        last_sync = latest_syncs.get(account.id)
        accounts_payload.append({
            "id": account.id,
            "label": account.label,
            "marketplace_code": marketplace_code,
            "is_default": bool(account.is_default),
            "is_active": bool(account.is_active),
            "has_credentials": account.has_credentials,
            "credential_expires_at": account.to_public_dict().get('credential_expires_at'),
            "connection_status": account.connection_status,
            "last_sync": last_sync.to_public_dict() if last_sync else None,
            "last_completed_at": catalog_completed_at.get(account.id),
            "sync_job": onboarding_jobs.get(account.id),
        })
    return render_template(
        "marketplace_listings_beta.html",
        accounts_payload=accounts_payload,
        initial_filters=filters,
        catalog_return_url=_catalog_return_url(filters),
        catalog_page=filters["page"],
        catalog_per_page=filters["per_page"],
        ozon_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ),
    )


@marketplace_listings_bp.route("/beta/<int:listing_id>")
@marketplace_listings_bp.route("/view/<int:listing_id>", endpoint="view")
@login_required
def beta_detail(listing_id: int):
    """Тестовая деталь товара: каналы одной общей карточки, read-only + существующие link-действия.

    С ``Accept: application/json`` тот же роут отдаёт полный bootstrap страницы,
    поэтому после действий со связью каналы обновляются без перезагрузки.
    """
    seller_id = _seller_id()
    if seller_id is None:
        if _wants_json():
            return jsonify({
                "success": False,
                "error": "Seller account required",
            }), 403
        return "Seller account required", 403
    try:
        listing = MarketplaceListingService.get_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        members = MarketplaceListingService.group_members(
            seller_id=seller_id,
            listing=listing,
        )
    except MarketplaceListingError as exc:
        return _error_response(exc)
    if _wants_json():
        response = jsonify({
            "success": True,
            **_beta_detail_payload(seller_id=seller_id, listing=listing),
            "members": members,
            "csrf": generate_csrf(),
        })
        response.headers["Cache-Control"] = "private, no-store"
        return response
    return render_template(
        "marketplace_listing_beta_detail.html",
        listing_id=listing.id,
        members=members,
        return_url=_detail_return_url(listing),
        ozon_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ),
    )


def _beta_detail_payload(*, seller_id: int, listing) -> Dict[str, Any]:
    """Общий read-only снимок карточки для beta-страницы."""
    warehouse_stocks = []
    if listing.marketplace and listing.marketplace.code == "ozon":
        warehouse_stocks = MarketplaceWarehouseService.list_listing_stocks(
            seller_id=seller_id,
            listing_id=listing.id,
        )
    try:
        product_link = MarketplaceProductLinkService.context(
            seller_id=seller_id,
            listing_id=listing.id,
            listing=listing,
        )
    except MarketplaceProductLinkError:
        product_link = None
    return {
        "listing": listing.to_public_dict(detail=True),
        "gallery": MarketplaceListingService.gallery_urls(listing=listing),
        "attribute_names": MarketplaceListingService.attribute_names(
            listing=listing,
        ),
        "warehouse_stocks": [row.to_public_dict() for row in warehouse_stocks],
        "product_link": product_link,
    }


@marketplace_listings_bp.route('/view/<int:listing_id>/link-candidates', methods=['GET'])
@login_required
def vue_link_candidates(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify(success=False, error='Seller account required'), 403
    try:
        if set(request.args) - {'q'} or any(
            len(request.args.getlist(key)) != 1 for key in request.args
        ):
            raise ValueError('Недопустимые параметры поиска')
        query = request.args.get('q', '')
        if len(query) > 200:
            raise ValueError('Поиск ограничен 200 символами')
        listing = MarketplaceListingService.get_listing(
            seller_id=seller_id, listing_id=listing_id,
        )
        if not listing.marketplace or listing.marketplace.code != 'ozon':
            raise ValueError('Ручная связь доступна только для Ozon')
        candidates = MarketplaceProductLinkService.search_candidates(
            seller_id=seller_id, listing_id=listing_id, query=query, limit=20,
        ) if listing.imported_product_id is None else []
    except (MarketplaceListingError, MarketplaceProductLinkError, ValueError) as exc:
        return _error_response(exc)
    return jsonify(
        success=True, listing_id=listing.id, link_version=listing.link_version,
        candidates=candidates,
    )


@marketplace_listings_bp.route("/api/facets")
@login_required
def facets_api():
    """Счётчики статусов и каналов одним агрегатом вместо 5+N запросов."""
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        filters = _listing_filters()
        filters.pop("page", None)
        filters.pop("per_page", None)
        facets = MarketplaceListingService.catalog_facets(
            seller_id=seller_id,
            **filters,
        )
    except (MarketplaceListingError, ValueError) as exc:
        return _error_response(exc)
    return jsonify({"success": True, **facets})


@marketplace_listings_bp.route("/api/groups")
@login_required
def groups_api():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        payload = MarketplaceListingService.list_catalog_groups(
            seller_id=seller_id,
            **_listing_filters(),
        )
    except (MarketplaceListingError, ValueError) as exc:
        return _error_response(exc)
    return jsonify({"success": True, **payload})


@marketplace_listings_bp.route("/api")
@login_required
def list_api():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        pagination = MarketplaceListingService.list_listings(
            seller_id=seller_id,
            **_listing_filters(),
        )
    except (MarketplaceListingError, ValueError) as exc:
        return _error_response(exc)
    return jsonify({
        "success": True,
        "items": [item.to_public_dict() for item in pagination.items],
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "pages": pagination.pages,
            "total": pagination.total,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        },
    })


@marketplace_listings_bp.route("/<int:listing_id>")
@login_required
def detail(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        listing = MarketplaceListingService.get_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        product_link = MarketplaceProductLinkService.context(
            seller_id=seller_id,
            listing_id=listing.id,
            query=request.args.get("link_search") or None,
            listing=listing,
        )
    except (MarketplaceListingError, MarketplaceProductLinkError) as exc:
        return _error_response(exc)
    canonical_content = None
    canonical_content_proposals = []
    if (
        listing.marketplace
        and listing.marketplace.code == "ozon"
        and current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
    ):
        try:
            canonical_content_proposals = (
                MarketplaceCanonicalContentService.list_for_listing(
                    seller_id=seller_id,
                    listing_id=listing.id,
                )
            )
        except MarketplaceCanonicalContentError as exc:
            canonical_content = {
                "proposal_allowed": False,
                "blocked_reasons": [exc.code],
                "error": str(exc),
                "fields": [],
                "differing_fields": [],
                "excluded_scopes": [],
            }
        if listing.canonical_link_status == "linked":
            try:
                canonical_content = MarketplaceCanonicalContentService.comparison(
                    seller_id=seller_id,
                    listing_id=listing.id,
                )
            except MarketplaceCanonicalContentError as exc:
                canonical_content = {
                    "proposal_allowed": False,
                    "blocked_reasons": [exc.code],
                    "error": str(exc),
                    "fields": [],
                    "differing_fields": [],
                    "excluded_scopes": [],
                }
        elif canonical_content_proposals and canonical_content is None:
            canonical_content = {
                "proposal_allowed": False,
                "blocked_reasons": ["canonical_link_unavailable"],
                "error": (
                    "Связь с общей карточкой сейчас отсутствует; новые diff "
                    "недоступны, но существующие proposal и rollback сохранены"
                ),
                "fields": [],
                "differing_fields": [],
                "excluded_scopes": [],
            }
    if _wants_json():
        warehouse_stocks = (
            MarketplaceWarehouseService.list_listing_stocks(
                seller_id=seller_id,
                listing_id=listing.id,
            )
            if listing.marketplace and listing.marketplace.code == "ozon"
            else []
        )
        return jsonify({
            "success": True,
            "listing": listing.to_public_dict(detail=True),
            "attribute_names": MarketplaceListingService.attribute_names(
                listing=listing,
            ),
            "warehouse_stocks": [row.to_public_dict() for row in warehouse_stocks],
            "product_link": product_link,
            "canonical_content": canonical_content,
            "canonical_content_proposals": [
                proposal.to_public_dict()
                for proposal in canonical_content_proposals
            ],
        })
    warehouse_stocks = []
    proposals = []
    if listing.marketplace and listing.marketplace.code == "ozon":
        warehouse_stocks = MarketplaceWarehouseService.list_listing_stocks(
            seller_id=seller_id,
            listing_id=listing.id,
            include_unavailable=True,
        )
        proposals = MarketplaceCommercialProposal.query.filter_by(
            seller_id=seller_id,
            account_id=listing.account_id,
            listing_id=listing.id,
        ).order_by(
            MarketplaceCommercialProposal.created_at.desc(),
            MarketplaceCommercialProposal.id.desc(),
        ).limit(20).all()
    return render_template(
        "marketplace_listing_detail.html",
        listing=listing,
        listing_preview=listing_display(listing),
        listing_data=listing.to_public_dict(detail=True),
        product_link=product_link,
        link_search=request.args.get("link_search", ""),
        warehouse_stocks=warehouse_stocks,
        commercial_proposals=proposals,
        return_url=_detail_return_url(listing),
        workspace_navigation={
            "overview_url": url_for(
                "marketplace_listings.view",
                listing_id=listing.id,
                return_to=_detail_return_url(listing),
            ),
            "management_url": url_for(
                "marketplace_listings.detail",
                listing_id=listing.id,
                return_to=_detail_return_url(listing),
            ),
            "return_url": _detail_return_url(listing),
            "mode": "management",
        },
        canonical_content=canonical_content,
        canonical_content_proposals=canonical_content_proposals,
        canonical_content_proposal_data=[
            proposal.to_public_dict()
            for proposal in canonical_content_proposals
        ],
        commercial_feature_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ),
        commercial_write_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
            and current_app.config.get(
                "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED",
                False,
            )
        ),
    )


@marketplace_listings_bp.route(
    "/<int:listing_id>/update-draft-context",
    methods=["GET"],
)
@login_required
def update_draft_context(listing_id: int):
    """Read-only local context for preparing an update of one exact Ozon listing."""
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({
            "success": False,
            "error": "Для работы с листингом нужна карточка продавца",
            "code": "seller_profile_required",
        }), 403
    if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
        return jsonify({
            "success": False,
            "error": "Подготовка черновиков Ozon отключена",
            "code": "ozon_feature_disabled",
        }), 404
    try:
        context = MarketplaceDraftService.existing_listing_update_draft_context(
            seller_id=seller_id,
            listing_id=listing_id,
        )
    except MarketplaceDraftError as exc:
        return _error_response(exc)

    response_data = {
        "success": True,
        "status": context["status"],
        "account_id": context["account_id"],
        "account_label": context["account_label"],
        "imported_product_id": context["imported_product_id"],
    }
    if context["status"] == "ready":
        response_data["expected_existing_listing"] = context[
            "expected_existing_listing"
        ]
    else:
        response_data["draft_url"] = url_for(
            "marketplace_drafts.detail",
            draft_id=context["draft_id"],
        )
    response = jsonify(response_data)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@marketplace_listings_bp.route(
    "/update-drafts/preview",
    methods=["POST"],
)
@login_required
def preview_update_drafts():
    """Read and sign an exact local preview for selected Ozon listings."""
    seller_id = _seller_id()
    if seller_id is None:
        return _existing_update_error(
            "Для работы с листингами нужна карточка продавца",
            status_code=403,
            code="seller_profile_required",
        )
    if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
        return _existing_update_error(
            "Подготовка черновиков Ozon отключена",
            status_code=404,
            code="ozon_feature_disabled",
        )
    try:
        user_id = _positive_native_id(
            getattr(current_user, "id", None), "user_id",
        )
        seller_id = _positive_native_id(seller_id, "seller_id")
        data = _existing_update_body(
            required={"account_id", "listing_ids"},
            allowed={"account_id", "listing_ids", "return_to"},
        )
        account_id = _positive_native_id(data["account_id"], "account_id")
        listing_ids = _existing_update_ids(data["listing_ids"])
        return_to = _existing_update_return_url(data, account_id)
        raw_preview = OzonExistingUpdateBatch.preview(
            seller_id=seller_id,
            account_id=account_id,
            listing_ids=listing_ids,
        )
        preview = _bounded_existing_update_document(
            raw_preview,
            account_id=account_id,
            listing_ids=listing_ids,
            allowed_statuses={"ready", "existing", "blocked"},
            require_success_draft=False,
        )
        preview["token"] = _issue_existing_update_token(
            user_id=user_id,
            seller_id=seller_id,
            account_id=account_id,
            listing_ids=listing_ids,
            digest=raw_preview.get("digest"),
            return_to=return_to,
        )
        encoded = json.dumps(
            {"success": True, "preview": preview},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _EXISTING_UPDATE_RESPONSE_MAX_BYTES:
            raise ValueError("Предпросмотр выбранных карточек превышает лимит")
        return _existing_update_json({"success": True, "preview": preview})
    except MarketplaceDraftError as exc:
        return _existing_update_error(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
        )
    except ValueError as exc:
        return _existing_update_error(str(exc))
    except Exception:
        logger.exception(
            "Ozon existing-update preview failed seller_id=%s",
            seller_id,
        )
        return _existing_update_error(
            "Не удалось проверить выбранные карточки. Перечитайте список.",
            status_code=500,
            code="existing_update_preview_failed",
        )


@marketplace_listings_bp.route(
    "/update-drafts/apply",
    methods=["POST"],
)
@login_required
def apply_update_drafts():
    """Create only the exact locally reviewed existing-listing drafts."""
    seller_id = _seller_id()
    if seller_id is None:
        return _existing_update_error(
            "Для работы с листингами нужна карточка продавца",
            status_code=403,
            code="seller_profile_required",
        )
    if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
        return _existing_update_error(
            "Подготовка черновиков Ozon отключена",
            status_code=404,
            code="ozon_feature_disabled",
        )
    try:
        user_id = _positive_native_id(
            getattr(current_user, "id", None), "user_id",
        )
        seller_id = _positive_native_id(seller_id, "seller_id")
        data = _existing_update_body(
            required={"token", "confirm_local"},
            allowed={"token", "confirm_local"},
        )
        if data["confirm_local"] is not True:
            raise ValueError("Подтвердите локальную подготовку выбранных черновиков")
        payload = _load_existing_update_token(
            data["token"],
            user_id=user_id,
            seller_id=seller_id,
        )
        raw_report = OzonExistingUpdateBatch.apply(
            seller_id=seller_id,
            account_id=payload["account_id"],
            listing_ids=payload["listing_ids"],
            expected_digest=payload["digest"],
        )
        report = _bounded_existing_update_document(
            raw_report,
            account_id=payload["account_id"],
            listing_ids=payload["listing_ids"],
            allowed_statuses={"created", "existing", "blocked"},
            require_success_draft=True,
        )
        successful_draft_ids = [
            row["draft_id"]
            for row in report["rows"]
            if row["status"] in {"created", "existing"}
        ]
        report["editor_url"] = None
        if successful_draft_ids:
            report["editor_url"] = url_for(
                "ozon_bulk_uploads.repair_draft_selection",
                account_id=payload["account_id"],
                draft_ids=",".join(str(value) for value in successful_draft_ids),
                return_to=payload["return_to"],
            )
        return _existing_update_json({"success": True, "report": report})
    except _ExistingUpdateReviewError as exc:
        return _existing_update_error(
            str(exc), status_code=exc.status_code, code=exc.code,
        )
    except MarketplaceDraftError as exc:
        return _existing_update_error(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
        )
    except ValueError as exc:
        return _existing_update_error(str(exc))
    except Exception:
        logger.exception(
            "Ozon existing-update apply failed seller_id=%s",
            seller_id,
        )
        return _existing_update_error(
            "Результат подготовки не подтверждён. Перечитайте выбранные карточки.",
            status_code=500,
            code="existing_update_apply_unknown",
        )


def _link_write_payload(data: Dict[str, Any], allowed: set) -> None:
    unknown = set(data) - allowed - {"csrf_token"}
    if unknown:
        raise ValueError("Неизвестные поля: " + ", ".join(sorted(unknown)))


def _canonical_feature() -> None:
    if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
        raise MarketplaceCanonicalContentNotFound(
            "Ozon-интеграция выключена",
            code="ozon_feature_disabled",
        )


def _canonical_failure(
    error: Exception,
    *,
    listing_id: Optional[int] = None,
):
    db.session.rollback()
    if _wants_json():
        return _error_response(error)
    target_listing_id = (
        getattr(error, "listing_id", None)
        or listing_id
    )
    flash(str(error), "error")
    if target_listing_id:
        return redirect(url_for(
            "marketplace_listings.detail",
            listing_id=target_listing_id,
            return_to=_detail_return_url(),
        ))
    return redirect(url_for("marketplace_listings.index"))


@marketplace_listings_bp.route("/<int:listing_id>/link", methods=["POST"])
@login_required
def link_product(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    data = _payload()
    try:
        _link_write_payload(
            data,
            {"imported_product_id", "expected_link_version"},
        )
        listing = MarketplaceProductLinkService.link(
            seller_id=seller_id,
            listing_id=listing_id,
            imported_product_id=_payload_integer(
                data,
                "imported_product_id",
                None,
            ),
            expected_link_version=_payload_integer(
                data,
                "expected_link_version",
                None,
            ),
            actor_user_id=getattr(current_user, "id", None),
        )
    except (MarketplaceProductLinkError, ValueError) as exc:
        db.session.rollback()
        return _error_response(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "listing": listing.to_public_dict(detail=True),
        })
    flash(
        "Ozon-листинг связан с общей внутренней карточкой; AI-парсинг и контент будут переиспользованы",
        "success",
    )
    return _redirect_to_detail(listing.id, listing=listing)


@marketplace_listings_bp.route("/<int:listing_id>/unlink", methods=["POST"])
@login_required
def unlink_product(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    data = _payload()
    try:
        _link_write_payload(data, {"expected_link_version"})
        listing = MarketplaceProductLinkService.unlink(
            seller_id=seller_id,
            listing_id=listing_id,
            expected_link_version=_payload_integer(
                data,
                "expected_link_version",
                None,
            ),
            actor_user_id=getattr(current_user, "id", None),
        )
    except (MarketplaceProductLinkError, ValueError) as exc:
        db.session.rollback()
        return _error_response(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "listing": listing.to_public_dict(detail=True),
        })
    flash("Связь с внутренней карточкой удалена", "success")
    return _redirect_to_detail(listing.id, listing=listing)


@marketplace_listings_bp.route("/<int:listing_id>/reconcile-link", methods=["POST"])
@login_required
def reconcile_product_link(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    data = _payload()
    try:
        _link_write_payload(data, set())
        listing, outcome = (
            MarketplaceProductLinkService.reconcile_listing_with_outcome(
                seller_id=seller_id,
                listing_id=listing_id,
            )
        )
    except (MarketplaceProductLinkError, ValueError) as exc:
        db.session.rollback()
        return _error_response(exc)
    # Занятый seller-lock значит «идёт фоновая сверка», а не «совпадений нет».
    busy = bool(outcome.get("busy")) and not listing.imported_product_id
    if _wants_json():
        return jsonify({
            "success": True,
            "busy": busy,
            "listing": listing.to_public_dict(detail=True),
        })
    if busy:
        flash(
            "Сейчас идёт фоновая сверка каталога — повторите через минуту",
            "info",
        )
        return redirect(url_for(
            "marketplace_listings.detail",
            listing_id=listing.id,
            return_to=_detail_return_url(listing),
        ))
    if listing.imported_product_id:
        flash("Найдена одна точная внутренняя карточка; связь создана", "success")
    elif listing.canonical_link_status == "ambiguous":
        flash("Найдено несколько точных совпадений — выберите карточку вручную", "warning")
    else:
        flash("Точного совпадения не найдено; выберите карточку вручную", "info")
    return _redirect_to_detail(listing.id, listing=listing)


@marketplace_listings_bp.route(
    "/<int:listing_id>/canonical-content-proposals",
    methods=["POST"],
)
@login_required
def create_canonical_content_proposal(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    data = _payload()
    try:
        _canonical_feature()
        _link_write_payload(data, {"fields"})
        fields = data.get("fields")
        if not request.is_json and fields not in (None, ""):
            raise ValueError("fields можно передать только JSON array")
        proposal = MarketplaceCanonicalContentService.create_proposal(
            seller_id=seller_id,
            listing_id=listing_id,
            created_by_user_id=getattr(current_user, "id", None),
            fields=fields if request.is_json else None,
        )
    except (MarketplaceCanonicalContentError, ValueError) as exc:
        return _canonical_failure(exc, listing_id=listing_id)
    if _wants_json():
        return jsonify({
            "success": True,
            "proposal": proposal.to_public_dict(detail=True),
        }), 201
    flash(
        "Ozon → общая карточка diff сохранён; до подтверждения ничего не изменено",
        "success",
    )
    return redirect(url_for(
        "marketplace_listings.detail",
        listing_id=proposal.listing_id,
        return_to=_detail_return_url(),
    ))


@marketplace_listings_bp.route(
    "/canonical-content-proposals/<int:proposal_id>/apply",
    methods=["POST"],
)
@login_required
def apply_canonical_content_proposal(proposal_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    data = _payload()
    try:
        _canonical_feature()
        _link_write_payload(
            data,
            {"expected_version", "confirm_apply", "note"},
        )
        if not _payload_boolean(data, "confirm_apply", False):
            raise ValueError("Apply требует confirm_apply=true")
        proposal = MarketplaceCanonicalContentService.apply_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
            expected_version=_payload_integer(
                data,
                "expected_version",
                None,
            ),
            reviewed_by_user_id=getattr(current_user, "id", None),
            note=data.get("note") or None,
        )
    except (MarketplaceCanonicalContentError, ValueError) as exc:
        return _canonical_failure(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "proposal": proposal.to_public_dict(detail=True),
        })
    flash(
        "Общая карточка обновлена локально. WB и Ozon не публиковались; "
        "их проекции теперь требуют отдельной проверки.",
        "success",
    )
    return redirect(url_for(
        "marketplace_listings.detail",
        listing_id=proposal.listing_id,
        return_to=_detail_return_url(),
    ))


@marketplace_listings_bp.route(
    "/canonical-content-proposals/<int:proposal_id>/reject",
    methods=["POST"],
)
@login_required
def reject_canonical_content_proposal(proposal_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    data = _payload()
    try:
        _link_write_payload(data, {"expected_version", "note"})
        proposal = MarketplaceCanonicalContentService.reject_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
            expected_version=_payload_integer(
                data,
                "expected_version",
                None,
            ),
            reviewed_by_user_id=getattr(current_user, "id", None),
            note=data.get("note") or None,
        )
    except (MarketplaceCanonicalContentError, ValueError) as exc:
        return _canonical_failure(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "proposal": proposal.to_public_dict(detail=True),
        })
    flash("Content proposal отклонён без изменения карточки", "success")
    return redirect(url_for(
        "marketplace_listings.detail",
        listing_id=proposal.listing_id,
        return_to=_detail_return_url(),
    ))


@marketplace_listings_bp.route(
    "/canonical-content-proposals/<int:proposal_id>/rollback",
    methods=["POST"],
)
@login_required
def rollback_canonical_content_proposal(proposal_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    data = _payload()
    try:
        _link_write_payload(data, {"expected_version", "confirm_rollback"})
        if not _payload_boolean(data, "confirm_rollback", False):
            raise ValueError("Rollback требует confirm_rollback=true")
        proposal = MarketplaceCanonicalContentService.rollback_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
            expected_version=_payload_integer(
                data,
                "expected_version",
                None,
            ),
            rolled_back_by_user_id=getattr(current_user, "id", None),
        )
    except (MarketplaceCanonicalContentError, ValueError) as exc:
        return _canonical_failure(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "proposal": proposal.to_public_dict(detail=True),
        })
    flash("Локальный common-content diff откачен", "success")
    return redirect(url_for(
        "marketplace_listings.detail",
        listing_id=proposal.listing_id,
        return_to=_detail_return_url(),
    ))


@marketplace_listings_bp.route("/accounts/<int:account_id>/sync", methods=["POST"])
@login_required
def sync_account(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
        if _wants_json():
            return jsonify({
                "success": False,
                "error": "Синхронизация Ozon отключена feature flag",
                "code": "ozon_feature_disabled",
            }), 404
        return render_template(
            "marketplace_listing_error.html",
            error="Синхронизация Ozon отключена feature flag",
        ), 404
    data = _payload()
    try:
        if set(data) - {'max_pages', 'force_restart', 'csrf_token'}:
            raise ValueError('Запрос загрузки содержит неизвестные поля')
        # Retain strict parsing for legacy API clients, but no browser/request
        # can expand the worker's one-page physical budget.
        pages = _payload_integer(data, 'max_pages', 5)
        if pages > MarketplaceListingService.MAX_SYNC_PAGES_PER_CALL:
            raise ValueError('Слишком большой пакет синхронизации')
        job = enqueue_account_sync(
            seller_id=seller_id,
            account_id=account_id,
            force_restart=_payload_boolean(data, "force_restart", False),
        )
    except (MarketplaceListingError, MarketplaceAccountError, ValueError) as exc:
        return _error_response(exc)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Ozon catalog sync failed seller_id=%s account_id=%s",
            seller_id,
            account_id,
        )
        return _error_response(
            MarketplaceListingError("Не удалось синхронизировать каталог Ozon"),
            500,
        )
    message = job['message']
    if _wants_json():
        return jsonify({
            "success": True,
            "message": message,
            "job": job,
            "status_url": url_for('marketplace_accounts.setup_status', account_id=account_id),
        }), 202
    flash(message, "success")
    return redirect(url_for("marketplace_listings.index", marketplace='ozon', account_id=account_id))


def register_marketplace_listing_routes(app) -> None:
    app.register_blueprint(marketplace_listings_bp)
