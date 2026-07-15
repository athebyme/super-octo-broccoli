"""Seller-facing unified marketplace listing read model."""

from typing import Any, Dict, Optional

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from models import MarketplaceCommercialProposal, db
from services.marketplace_accounts import MarketplaceAccountService
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


marketplace_listings_bp = Blueprint(
    "marketplace_listings",
    __name__,
    url_prefix="/marketplaces/listings",
)


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


def _listing_filters() -> Dict[str, Any]:
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
        "page": _integer(request.args.get("page"), "page", 1),
        "per_page": _integer(request.args.get("per_page"), "per_page", 50),
    }


@marketplace_listings_bp.route("/")
@login_required
def index():
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
        latest_syncs = MarketplaceListingService.latest_syncs(
            seller_id=seller_id,
        )
    except (MarketplaceListingError, ValueError) as exc:
        return _error_response(exc)
    return render_template(
        "marketplace_listings.html",
        pagination=pagination,
        listings=pagination.items,
        accounts=accounts,
        latest_syncs=latest_syncs,
        filters=filters,
        ozon_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ),
    )


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
        listing_data=listing.to_public_dict(detail=True),
        product_link=product_link,
        link_search=request.args.get("link_search", ""),
        warehouse_stocks=warehouse_stocks,
        commercial_proposals=proposals,
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
    return redirect(url_for("marketplace_listings.detail", listing_id=listing.id))


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
    return redirect(url_for("marketplace_listings.detail", listing_id=listing.id))


@marketplace_listings_bp.route("/<int:listing_id>/reconcile-link", methods=["POST"])
@login_required
def reconcile_product_link(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    data = _payload()
    try:
        _link_write_payload(data, set())
        listing = MarketplaceProductLinkService.reconcile_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
    except (MarketplaceProductLinkError, ValueError) as exc:
        db.session.rollback()
        return _error_response(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "listing": listing.to_public_dict(detail=True),
        })
    if listing.imported_product_id:
        flash("Найдена одна точная внутренняя карточка; связь создана", "success")
    elif listing.canonical_link_status == "ambiguous":
        flash("Найдено несколько точных совпадений — выберите карточку вручную", "warning")
    else:
        flash("Точного совпадения не найдено; выберите карточку вручную", "info")
    return redirect(url_for("marketplace_listings.detail", listing_id=listing.id))


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
        run = MarketplaceListingService.sync_ozon_account(
            seller_id=seller_id,
            account_id=account_id,
            max_pages=_payload_integer(data, "max_pages", 5),
            force_restart=_payload_boolean(data, "force_restart", False),
        )
    except (MarketplaceListingError, ValueError) as exc:
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
    message = (
        "Каталог Ozon полностью синхронизирован"
        if run.status == "completed"
        else "Синхронизация сохранена; продолжите следующий пакет"
    )
    if _wants_json():
        return jsonify({
            "success": True,
            "message": message,
            "sync": run.to_public_dict(),
        })
    flash(message, "success")
    return redirect(url_for("marketplace_listings.index", account_id=account_id))


def register_marketplace_listing_routes(app) -> None:
    app.register_blueprint(marketplace_listings_bp)
