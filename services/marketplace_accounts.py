"""Seller-scoped marketplace account lifecycle.

The service owns validation, encryption, tenant scope and connection health
persistence. Adapters receive only an in-memory credential DTO after ownership
has been proven with ``account_id + seller_id``.
"""

from datetime import datetime
from typing import Any, Dict, Optional, Tuple
import json

from sqlalchemy.exc import IntegrityError

from models import (
    Marketplace,
    MarketplaceCredentialEncryptionError,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_adapters import (
    ConnectionCheck,
    MarketplaceAdapterError,
    MarketplaceCredentials,
    get_marketplace_registry,
)


class MarketplaceAccountError(RuntimeError):
    status_code = 400
    code = "marketplace_account_error"


class MarketplaceAccountValidationError(MarketplaceAccountError):
    status_code = 400
    code = "invalid_marketplace_account"


class MarketplaceAccountNotFound(MarketplaceAccountError):
    status_code = 404
    code = "marketplace_account_not_found"


class MarketplaceAccountConflict(MarketplaceAccountError):
    status_code = 409
    code = "marketplace_account_conflict"


class MarketplaceAccountConfigurationError(MarketplaceAccountError):
    status_code = 503
    code = "marketplace_account_configuration_error"


class MarketplaceAccountService:
    MAX_ACCOUNTS_PER_MARKETPLACE = 10
    MAX_LABEL_LENGTH = 120
    MAX_EXTERNAL_ACCOUNT_ID_LENGTH = 200
    ALLOWED_CONNECTION_STATUSES = {
        "unchecked",
        "connected",
        "invalid",
        "limited",
        "error",
        "disconnected",
    }

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceAccountValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _bounded_text(
        value: Any,
        field_name: str,
        *,
        maximum: int,
        required: bool = True,
    ) -> str:
        if not isinstance(value, str):
            raise MarketplaceAccountValidationError(
                f"{field_name} должен быть строкой"
            )
        normalized = value.strip()
        if required and not normalized:
            raise MarketplaceAccountValidationError(f"{field_name} обязателен")
        if len(normalized) > maximum:
            raise MarketplaceAccountValidationError(
                f"{field_name} длиннее {maximum} символов"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise MarketplaceAccountValidationError(
                f"{field_name} содержит управляющие символы"
            )
        return normalized

    @classmethod
    def _marketplace(cls, code: str) -> Marketplace:
        marketplace = Marketplace.query.filter_by(code=code, is_active=True).first()
        if marketplace is None:
            raise MarketplaceAccountConfigurationError(
                f"Маркетплейс {code} не инициализирован миграцией"
            )
        return marketplace

    @classmethod
    def get_owned_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
        marketplace_code: Optional[str] = None,
    ) -> SellerMarketplaceAccount:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        query = SellerMarketplaceAccount.query.filter_by(
            id=account_id,
            seller_id=seller_id,
        )
        if marketplace_code:
            query = query.join(Marketplace).filter(
                Marketplace.code == marketplace_code,
            )
        account = query.first()
        if account is None:
            raise MarketplaceAccountNotFound("Подключение не найдено")
        return account

    @classmethod
    def list_accounts(
        cls,
        *,
        seller_id: int,
        marketplace_code: Optional[str] = None,
    ) -> list:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        query = SellerMarketplaceAccount.query.filter_by(seller_id=seller_id)
        if marketplace_code:
            query = query.join(Marketplace).filter(
                Marketplace.code == marketplace_code,
            )
        return query.order_by(
            SellerMarketplaceAccount.is_default.desc(),
            SellerMarketplaceAccount.created_at.asc(),
            SellerMarketplaceAccount.id.asc(),
        ).all()

    @classmethod
    def save_ozon_account(
        cls,
        *,
        seller_id: int,
        external_account_id: Any,
        label: Any,
        api_key: Optional[Any],
        is_default: bool = False,
        account_id: Optional[int] = None,
    ) -> SellerMarketplaceAccount:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        if not isinstance(is_default, bool):
            raise MarketplaceAccountValidationError(
                "is_default должен быть boolean"
            )
        external_account_id = cls._bounded_text(
            external_account_id,
            "Client-Id",
            maximum=cls.MAX_EXTERNAL_ACCOUNT_ID_LENGTH,
        )
        label = cls._bounded_text(
            label,
            "Название подключения",
            maximum=cls.MAX_LABEL_LENGTH,
        )
        normalized_api_key = None
        if api_key not in (None, ""):
            normalized_api_key = cls._bounded_text(
                api_key,
                "API key",
                maximum=2000,
            )

        marketplace = cls._marketplace("ozon")
        if account_id is None:
            account = None
            existing_count = SellerMarketplaceAccount.query.filter_by(
                seller_id=seller_id,
                marketplace_id=marketplace.id,
            ).count()
            if existing_count >= cls.MAX_ACCOUNTS_PER_MARKETPLACE:
                raise MarketplaceAccountConflict(
                    "Достигнут лимит подключений Ozon для продавца"
                )
        else:
            account = cls.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )

        duplicate_query = SellerMarketplaceAccount.query.filter_by(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            external_account_id=external_account_id,
        )
        if account is not None:
            duplicate_query = duplicate_query.filter(
                SellerMarketplaceAccount.id != account.id,
            )
        if duplicate_query.first() is not None:
            raise MarketplaceAccountConflict(
                "Этот кабинет Ozon уже подключён"
            )

        is_new = account is None
        if is_new:
            if normalized_api_key is None:
                raise MarketplaceAccountValidationError(
                    "API key обязателен для нового подключения"
                )
            account = SellerMarketplaceAccount(
                seller_id=seller_id,
                marketplace_id=marketplace.id,
                external_account_id=external_account_id,
                label=label,
                is_active=True,
                is_default=False,
                connection_status="unchecked",
                capabilities_json="[]",
                roles_json="[]",
                settings_json="{}",
            )
            db.session.add(account)

        identity_changed = account.external_account_id != external_account_id
        credential_changed = normalized_api_key is not None
        account.external_account_id = external_account_id
        account.label = label
        account.is_active = True
        if credential_changed:
            try:
                account.set_credentials({"api_key": normalized_api_key})
            except MarketplaceCredentialEncryptionError as exc:
                db.session.rollback()
                raise MarketplaceAccountConfigurationError(str(exc)) from None
            except ValueError as exc:
                db.session.rollback()
                raise MarketplaceAccountValidationError(str(exc)) from None

        if identity_changed or credential_changed:
            account.connection_status = "unchecked"
            account.connection_checked_at = None
            account.provider_request_id = None
            account.last_error_code = None
            account.last_error_message = None
            account.capabilities_json = "[]"
            account.roles_json = "[]"

        other_default = SellerMarketplaceAccount.query.filter_by(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            is_default=True,
        )
        if account.id is not None:
            other_default = other_default.filter(
                SellerMarketplaceAccount.id != account.id,
            )
        should_default = is_default or other_default.first() is None
        if should_default:
            SellerMarketplaceAccount.query.filter_by(
                seller_id=seller_id,
                marketplace_id=marketplace.id,
            ).update(
                {SellerMarketplaceAccount.is_default: False},
                synchronize_session="fetch",
            )
            account.is_default = True

        account.version = 1 if is_new else (account.version or 0) + 1
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceAccountConflict(
                "Этот кабинет Ozon уже подключён"
            ) from None
        except Exception:
            db.session.rollback()
            raise
        return account

    @classmethod
    def check_connection(
        cls,
        *,
        seller_id: int,
        account_id: int,
        registry=None,
        now: Optional[datetime] = None,
    ) -> Tuple[SellerMarketplaceAccount, ConnectionCheck]:
        account = cls.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
        )
        if not account.has_credentials:
            raise MarketplaceAccountValidationError(
                "У подключения нет сохранённых credentials"
            )
        try:
            secret = account.get_credentials()
            credentials = MarketplaceCredentials(
                external_account_id=account.external_account_id,
                api_key=secret["api_key"],
            )
            del secret
        except KeyError:
            raise MarketplaceAccountConfigurationError(
                "Credentials подключения имеют неизвестный формат"
            ) from None
        except MarketplaceCredentialEncryptionError as exc:
            raise MarketplaceAccountConfigurationError(str(exc)) from None
        except ValueError as exc:
            raise MarketplaceAccountValidationError(str(exc)) from None

        registry = registry or get_marketplace_registry()
        try:
            result = registry.get(account.marketplace.code).check_connection(
                credentials,
            )
        except MarketplaceAdapterError:
            result = ConnectionCheck(
                ok=False,
                status="error",
                external_account_id=account.external_account_id,
                error_code="adapter_unavailable",
                error_message="Адаптер маркетплейса недоступен",
            )
        except Exception:
            # Never bubble an arbitrary provider/adapter exception into Flask
            # logging: third-party exception text is not a trusted secret-free
            # boundary.
            result = ConnectionCheck(
                ok=False,
                status="error",
                external_account_id=account.external_account_id,
                error_code="adapter_connection_check_failed",
                error_message="Не удалось проверить подключение маркетплейса",
            )

        checked_at = now or datetime.utcnow()
        status = result.status if result.status in cls.ALLOWED_CONNECTION_STATUSES else "error"
        account.connection_status = status
        account.connection_checked_at = checked_at
        account.capabilities_json = json.dumps(
            sorted(set(result.capabilities)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        account.roles_json = json.dumps(
            sorted(set(result.roles)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        account.credential_expires_at = result.expires_at
        account.provider_request_id = cls._optional_status_text(
            result.provider_request_id,
            maximum=200,
        )
        account.last_error_code = cls._optional_status_text(
            result.error_code,
            maximum=100,
        )
        account.last_error_message = cls._optional_status_text(
            result.error_message,
            maximum=1000,
            redactions=(credentials.api_key,),
        )
        account.version = (account.version or 0) + 1
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return account, result

    @classmethod
    def _optional_status_text(
        cls,
        value: Any,
        *,
        maximum: int,
        redactions: tuple = (),
    ) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value)
        for secret in redactions:
            if secret:
                text = text.replace(str(secret), "[redacted]")
        text = " ".join(
            text.replace("\x00", " ").replace("\r", " ").replace("\n", " ").split()
        )
        return text[:maximum] or None

    @classmethod
    def set_default(
        cls,
        *,
        seller_id: int,
        account_id: int,
    ) -> SellerMarketplaceAccount:
        account = cls.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
        )
        if not account.is_active or not account.has_credentials:
            raise MarketplaceAccountConflict(
                "Отключённый кабинет нельзя сделать основным"
            )
        SellerMarketplaceAccount.query.filter_by(
            seller_id=account.seller_id,
            marketplace_id=account.marketplace_id,
        ).update(
            {SellerMarketplaceAccount.is_default: False},
            synchronize_session="fetch",
        )
        account.is_default = True
        account.version = (account.version or 0) + 1
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return account

    @classmethod
    def disconnect(
        cls,
        *,
        seller_id: int,
        account_id: int,
    ) -> SellerMarketplaceAccount:
        account = cls.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
        )
        was_default = bool(account.is_default)
        account.clear_credentials()
        account.is_active = False
        account.is_default = False
        account.connection_status = "disconnected"
        account.capabilities_json = "[]"
        account.roles_json = "[]"
        account.provider_request_id = None
        account.last_error_code = None
        account.last_error_message = None
        account.version = (account.version or 0) + 1

        if was_default:
            SellerMarketplaceAccount.query.filter_by(
                seller_id=account.seller_id,
                marketplace_id=account.marketplace_id,
            ).update(
                {SellerMarketplaceAccount.is_default: False},
                synchronize_session="fetch",
            )
            replacement = SellerMarketplaceAccount.query.filter(
                SellerMarketplaceAccount.seller_id == account.seller_id,
                SellerMarketplaceAccount.marketplace_id == account.marketplace_id,
                SellerMarketplaceAccount.id != account.id,
                SellerMarketplaceAccount.is_active.is_(True),
                SellerMarketplaceAccount._credentials_encrypted.isnot(None),
            ).order_by(
                SellerMarketplaceAccount.created_at.asc(),
                SellerMarketplaceAccount.id.asc(),
            ).first()
            if replacement is not None:
                replacement.is_default = True
                replacement.version = (replacement.version or 0) + 1
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return account
