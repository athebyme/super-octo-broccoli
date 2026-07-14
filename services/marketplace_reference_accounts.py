"""Admin-owned marketplace reference credential lifecycle."""

from datetime import datetime
from typing import Any, Optional, Tuple
import json

from models import (
    Marketplace,
    MarketplaceCredentialEncryptionError,
    MarketplaceReferenceAccount,
    db,
)
from services.marketplace_adapters import (
    ConnectionCheck,
    MarketplaceCredentials,
    get_marketplace_registry,
)


class MarketplaceReferenceAccountError(RuntimeError):
    status_code = 400
    code = "marketplace_reference_account_error"


class MarketplaceReferenceAccountNotFound(MarketplaceReferenceAccountError):
    status_code = 404
    code = "marketplace_reference_account_not_found"


class MarketplaceReferenceAccountConfigurationError(MarketplaceReferenceAccountError):
    status_code = 503
    code = "marketplace_reference_account_configuration_error"


class MarketplaceReferenceAccountService:
    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceReferenceAccountError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _text(value: Any, field_name: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise MarketplaceReferenceAccountError(
                f"{field_name} должен быть строкой"
            )
        value = value.strip()
        if not value or len(value) > maximum:
            raise MarketplaceReferenceAccountError(
                f"Некорректное значение {field_name}"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise MarketplaceReferenceAccountError(
                f"{field_name} содержит управляющие символы"
            )
        return value

    @classmethod
    def marketplace(cls, marketplace_id: int) -> Marketplace:
        marketplace_id = cls._positive_integer(marketplace_id, "marketplace_id")
        marketplace = Marketplace.query.filter_by(
            id=marketplace_id,
            code="ozon",
            is_active=True,
        ).first()
        if marketplace is None:
            raise MarketplaceReferenceAccountNotFound(
                "Активный Ozon marketplace не найден"
            )
        return marketplace

    @classmethod
    def get(cls, marketplace_id: int) -> MarketplaceReferenceAccount:
        marketplace = cls.marketplace(marketplace_id)
        account = MarketplaceReferenceAccount.query.filter_by(
            marketplace_id=marketplace.id,
        ).first()
        if account is None:
            raise MarketplaceReferenceAccountNotFound(
                "Reference account Ozon не настроен"
            )
        return account

    @classmethod
    def save(
        cls,
        *,
        marketplace_id: int,
        external_account_id: Any,
        api_key: Optional[Any],
    ) -> MarketplaceReferenceAccount:
        marketplace = cls.marketplace(marketplace_id)
        external_account_id = cls._text(
            external_account_id,
            "Client-Id",
            200,
        )
        account = MarketplaceReferenceAccount.query.filter_by(
            marketplace_id=marketplace.id,
        ).first()
        if account is None:
            if api_key in (None, ""):
                raise MarketplaceReferenceAccountError(
                    "API key обязателен для нового reference account"
                )
            account = MarketplaceReferenceAccount(
                marketplace_id=marketplace.id,
                external_account_id=external_account_id,
                connection_status="unchecked",
                roles_json="[]",
            )
            db.session.add(account)
        identity_changed = account.external_account_id != external_account_id
        account.external_account_id = external_account_id
        if api_key not in (None, ""):
            api_key = cls._text(api_key, "API key", 2000)
            try:
                account.set_credentials({"api_key": api_key})
            except MarketplaceCredentialEncryptionError as exc:
                db.session.rollback()
                raise MarketplaceReferenceAccountConfigurationError(str(exc)) from None
            except ValueError as exc:
                db.session.rollback()
                raise MarketplaceReferenceAccountError(str(exc)) from None
            identity_changed = True
        if identity_changed:
            account.connection_status = "unchecked"
            account.connection_checked_at = None
            account.credential_expires_at = None
            account.roles_json = "[]"
            account.provider_request_id = None
            account.last_error_code = None
            account.last_error_message = None
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return account

    @classmethod
    def check(
        cls,
        *,
        marketplace_id: int,
        registry=None,
        now: Optional[datetime] = None,
    ) -> Tuple[MarketplaceReferenceAccount, ConnectionCheck]:
        account = cls.get(marketplace_id)
        if not account.has_credentials:
            raise MarketplaceReferenceAccountError(
                "Reference account не содержит API key"
            )
        try:
            secret = account.get_credentials()
            credentials = MarketplaceCredentials(
                external_account_id=account.external_account_id,
                api_key=secret["api_key"],
            )
            del secret
        except (KeyError, ValueError, MarketplaceCredentialEncryptionError):
            raise MarketplaceReferenceAccountConfigurationError(
                "Reference credentials невозможно прочитать"
            ) from None

        try:
            adapter = (registry or get_marketplace_registry()).get("ozon")
            result = adapter.check_connection(credentials)
        except Exception:
            result = ConnectionCheck(
                ok=False,
                status="error",
                external_account_id=account.external_account_id,
                error_code="reference_connection_check_failed",
                error_message="Не удалось проверить Ozon reference account",
            )

        account.connection_status = (
            result.status
            if result.status in {"connected", "invalid", "error"}
            else "error"
        )
        account.connection_checked_at = now or datetime.utcnow()
        account.credential_expires_at = result.expires_at
        account.roles_json = json.dumps(
            sorted(set(result.roles)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        account.provider_request_id = cls._status_text(
            result.provider_request_id,
            200,
        )
        account.last_error_code = cls._status_text(result.error_code, 100)
        account.last_error_message = cls._status_text(
            result.error_message,
            1000,
            redact=credentials.api_key,
        )
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return account, result

    @staticmethod
    def _status_text(
        value: Any,
        maximum: int,
        *,
        redact: Optional[str] = None,
    ) -> Optional[str]:
        if value in (None, ""):
            return None
        value = str(value)
        if redact:
            value = value.replace(redact, "[redacted]")
        value = " ".join(
            value.replace("\x00", " ").replace("\r", " ").replace("\n", " ").split()
        )
        return value[:maximum] or None

    @classmethod
    def disconnect(cls, *, marketplace_id: int) -> MarketplaceReferenceAccount:
        account = cls.get(marketplace_id)
        account.clear_credentials()
        account.connection_status = "unchecked"
        account.connection_checked_at = None
        account.credential_expires_at = None
        account.roles_json = "[]"
        account.provider_request_id = None
        account.last_error_code = None
        account.last_error_message = None
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return account
