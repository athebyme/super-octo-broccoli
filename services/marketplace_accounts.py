"""Seller-scoped marketplace account lifecycle.

The service owns validation, encryption, tenant scope and connection health
persistence. Adapters receive only an in-memory credential DTO after ownership
has been proven with ``account_id + seller_id``.
"""

from datetime import datetime
from contextlib import contextmanager, ExitStack
from typing import Any, Dict, Optional, Tuple
import json

from sqlalchemy.exc import IntegrityError

from models import (
    Marketplace,
    MarketplaceCanonicalContentProposal,
    MarketplaceCommercialProposal,
    MarketplaceCredentialEncryptionError,
    MarketplaceMediaOperation,
    MarketplaceMediaPublication,
    MarketplaceOperation,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_operation_locks import (
    _try_operation_lock,
    release_account_operation_lock,
    try_account_operation_lock,
)
from services.marketplace_account_history import snapshot as account_snapshot, append_event, validate_actor
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


class MarketplaceAccountVersionConflict(MarketplaceAccountConflict):
    code = "marketplace_account_version_conflict"


class MarketplaceAccountConfigurationError(MarketplaceAccountError):
    status_code = 503
    code = "marketplace_account_configuration_error"


class MarketplaceAccountService:
    MAX_ACCOUNTS_PER_MARKETPLACE = 10
    MAX_LABEL_LENGTH = 120
    MAX_EXTERNAL_ACCOUNT_ID_LENGTH = 200
    OZON_VAT_VALUES = {
        "0", "0.05", "0.07", "0.1", "0.10", "0.2", "0.20", "0.22",
    }
    ALLOWED_CONNECTION_STATUSES = {
        "unchecked",
        "connected",
        "invalid",
        "limited",
        "error",
        "disconnected",
    }
    RECONCILIATION_REQUIRED_STATUSES = {
        "submitting",
        "submitted",
        "polling",
        "uncertain",
    }
    ACCOUNT_MUTATION_BLOCKING_STATUSES = RECONCILIATION_REQUIRED_STATUSES | {
        "queued",
    }
    MEDIA_OPERATION_BLOCKING_STATUSES = {
        "queued",
        "preflighting",
        "submitting",
        "reconciling",
        "uncertain",
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
        cls, *, seller_id, external_account_id, label, api_key, is_default=False,
        account_id=None, default_vat=None, actor_user_id=None,
    ):
        cls._require_clean_session()
        seller_id = cls._positive_integer(seller_id, 'seller_id')
        marketplace = cls._marketplace('ozon')
        with cls._group_claim(seller_id, marketplace.id):
            validate_actor(seller_id, actor_user_id)
            return cls._save_ozon_account_locked(seller_id=seller_id,
                external_account_id=external_account_id, label=label, api_key=api_key,
                is_default=is_default, account_id=account_id, default_vat=default_vat,
                actor_user_id=actor_user_id)

    @classmethod
    @contextmanager
    def _group_claim(cls, seller_id, marketplace_id):
        """Serialize bounded default fan-out with every affected account writer."""
        cls._require_clean_session()
        group = _try_operation_lock('account-settings-seller', seller_id)
        if group is None:
            raise MarketplaceAccountConflict('Настройки магазинов уже меняются. Повторите после обновления страницы.')
        with ExitStack() as stack:
            stack.callback(group.close)
            ids = [row[0] for row in db.session.query(SellerMarketplaceAccount.id).filter_by(
                seller_id=seller_id, marketplace_id=marketplace_id).order_by(SellerMarketplaceAccount.id).limit(cls.MAX_ACCOUNTS_PER_MARKETPLACE + 1)]
            db.session.rollback()
            if len(ids) > cls.MAX_ACCOUNTS_PER_MARKETPLACE:
                raise MarketplaceAccountConflict('Слишком много магазинов для безопасного изменения. Обратитесь к администратору.')
            for identity in ids:
                claim = try_account_operation_lock(identity)
                if claim is None:
                    raise MarketplaceAccountConflict('Один из магазинов сейчас сверяется с Ozon. Повторите через несколько секунд.')
                stack.callback(claim.close)
            try:
                yield
            except Exception:
                db.session.rollback()
                raise

    @classmethod
    def save_settings(cls, *, seller_id, account_id, external_account_id, label,
                      expected_version, default_vat=None, actor_user_id=None):
        cls._require_clean_session()
        seller_id = cls._positive_integer(seller_id, 'seller_id')
        account_id = cls._positive_integer(account_id, 'account_id')
        cls._reviewed_version(expected_version)
        label = cls._bounded_text(label, 'Название магазина', maximum=cls.MAX_LABEL_LENGTH)
        external_account_id = cls._bounded_text(external_account_id, 'Client-Id', maximum=cls.MAX_EXTERNAL_ACCOUNT_ID_LENGTH)
        if default_vat is not None and (not isinstance(default_vat, str) or default_vat not in cls.OZON_VAT_VALUES | {''}):
            raise MarketplaceAccountValidationError('Выберите поддерживаемую ставку НДС для новых карточек.')
        cls.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
        claim = try_account_operation_lock(account_id)
        if claim is None:
            raise MarketplaceAccountConflict('Магазин сейчас сверяется с Ozon. Повторите через несколько секунд.')
        try:
            db.session.expire_all()
            account = cls.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
            cls._reviewed_version(expected_version, account)
            validate_actor(seller_id, actor_user_id)
            if account.external_account_id != external_account_id:
                raise MarketplaceAccountConflict('Настройки относятся к другому Client-Id. Откройте нужный магазин.')
            try:
                settings = json.loads(account.settings_json or '{}')
            except (TypeError, ValueError):
                raise MarketplaceAccountConfigurationError('Сохранённые настройки повреждены. Обратитесь к администратору.') from None
            if not isinstance(settings, dict):
                raise MarketplaceAccountConfigurationError('Сохранённые настройки повреждены. Обратитесь к администратору.')
            before = account_snapshot(account)
            if default_vat is not None:
                if default_vat:
                    settings['default_vat'] = default_vat
                else:
                    settings.pop('default_vat', None)
            if account.label == label and settings == json.loads(account.settings_json or '{}'):
                return account
            account.label = label
            account.settings_json = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            account.version += 1
            append_event(account, before, 'settings_changed', actor_user_id)
            db.session.commit()
            return account
        except Exception:
            db.session.rollback()
            raise
        finally:
            claim.close()

    @staticmethod
    def _require_clean_session():
        session = db.session()
        if session.new or session.dirty or session.deleted:
            raise MarketplaceAccountConflict('Сначала завершите текущее изменение магазина.')
        if session.in_transaction() and db.engine.dialect.name == 'sqlite':
            if session.connection().connection.driver_connection.in_transaction:
                raise MarketplaceAccountConflict('Сначала завершите текущую транзакцию.')

    @staticmethod
    def _reviewed_version(expected_version, account=None):
        if type(expected_version) is not int or expected_version <= 0:
            raise MarketplaceAccountValidationError('Не удалось подтвердить просмотренную версию магазина. Перечитайте настройки.')
        if account is not None and account.version != expected_version:
            raise MarketplaceAccountVersionConflict('Настройки магазина изменились. Перечитайте текущее состояние перед сохранением.')

    @classmethod
    def _save_ozon_account_locked(
        cls,
        *,
        seller_id: int,
        external_account_id: Any,
        label: Any,
        api_key: Optional[Any],
        is_default: bool = False,
        account_id: Optional[int] = None,
        default_vat: Optional[Any] = None,
        actor_user_id: Optional[int] = None,
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
        normalized_default_vat = None
        if default_vat not in (None, ""):
            normalized_default_vat = cls._bounded_text(
                default_vat,
                "Ставка НДС",
                maximum=10,
            )
            if normalized_default_vat not in cls.OZON_VAT_VALUES:
                raise MarketplaceAccountValidationError(
                    "Ставка НДС не входит в поддерживаемый Ozon enum"
                )

        marketplace = cls._marketplace("ozon")
        if account_id is None:
            return cls._save_normalized_ozon_account(
                seller_id=seller_id,
                marketplace=marketplace,
                account=None,
                external_account_id=external_account_id,
                label=label,
                normalized_api_key=normalized_api_key,
                is_default=is_default,
                default_vat=normalized_default_vat,
                actor_user_id=actor_user_id,
            )

        account_id = cls._positive_integer(account_id, "account_id")
        cls.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
            marketplace_code="ozon",
        )
        db.session.expire_all()
        account = cls.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
            marketplace_code="ozon",
        )
        if account.external_account_id != external_account_id:
            raise MarketplaceAccountConflict(
                "Client-Id существующего кабинета нельзя заменить: "
                "для другого магазина создайте отдельное подключение. "
                "Так каталог и история останутся у своего магазина."
            )
        blocking = MarketplaceOperation.query.filter(
            MarketplaceOperation.seller_id == seller_id,
            MarketplaceOperation.account_id == account.id,
            MarketplaceOperation.status.in_(
                cls.ACCOUNT_MUTATION_BLOCKING_STATUSES
            ),
        ).first()
        blocking_media = MarketplaceMediaOperation.query.filter(
            MarketplaceMediaOperation.seller_id == seller_id,
            MarketplaceMediaOperation.account_id == account.id,
            MarketplaceMediaOperation.status.in_(
                cls.MEDIA_OPERATION_BLOCKING_STATUSES
            ),
        ).first()
        pending_commercial = MarketplaceCommercialProposal.query.filter(
            MarketplaceCommercialProposal.seller_id == seller_id,
            MarketplaceCommercialProposal.account_id == account.id,
            MarketplaceCommercialProposal.status.in_((
                "pending_review",
                "approved",
                "applying",
                "uncertain",
            )),
        ).first()
        pending_canonical_content = (
            MarketplaceCanonicalContentProposal.query.filter_by(
                seller_id=seller_id,
                account_id=account.id,
                status="pending_review",
            ).first()
        )
        if (
            blocking is not None
            or blocking_media is not None
            or pending_commercial is not None
            or pending_canonical_content is not None
        ):
            raise MarketplaceAccountConflict(
                "Настройки кабинета пока нельзя менять: отправка карточек, фото "
                "или подтверждение изменений ещё не завершены. "
                "Дождитесь сверки результата с Ozon."
            )
        return cls._save_normalized_ozon_account(
            seller_id=seller_id,
            marketplace=marketplace,
            account=account,
            external_account_id=external_account_id,
            label=label,
            normalized_api_key=normalized_api_key,
            is_default=is_default,
            default_vat=normalized_default_vat,
            actor_user_id=actor_user_id,
        )

    @classmethod
    def rotate_ozon_key(cls, *, seller_id, account_id, external_account_id, api_key, expected_version, actor_user_id=None):
        """Replace only the key of the same cabinet, including during recovery.

        An account claim excludes physical writes/reconciliation in flight.
        Durable pending/uncertain rows remain unchanged; they need the new key
        for their read-only reconciliation, not a second provider submission.
        """
        cls._require_clean_session()
        seller_id = cls._positive_integer(seller_id, 'seller_id')
        account_id = cls._positive_integer(account_id, 'account_id')
        external_account_id = cls._bounded_text(external_account_id, 'Client-Id', maximum=cls.MAX_EXTERNAL_ACCOUNT_ID_LENGTH)
        key = cls._bounded_text(api_key, 'API key', maximum=2000)
        if type(expected_version) is not int or expected_version <= 0:
            raise MarketplaceAccountValidationError('Не удалось подтвердить просмотренную версию магазина. Перечитайте настройки перед заменой ключа.')
        cls.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
        claim = try_account_operation_lock(account_id)
        if claim is None:
            raise MarketplaceAccountConflict('Кабинет сейчас сверяется с Ozon. Повторите замену ключа через несколько секунд.')
        try:
            db.session.expire_all()
            account = cls.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
            if account.version != expected_version:
                raise MarketplaceAccountVersionConflict('Настройки магазина изменились в другой вкладке или во время проверки. Новый ключ не сохранён. Перечитайте настройки, проверьте состояние магазина и подтвердите замену снова.')
            if account.external_account_id != external_account_id:
                raise MarketplaceAccountConflict('Новый ключ должен принадлежать тому же Client-Id. Другой магазин подключается отдельно.')
            validate_actor(seller_id, actor_user_id)
            before = account_snapshot(account)
            try:
                account.set_credentials({'api_key': key})
            except MarketplaceCredentialEncryptionError as exc:
                raise MarketplaceAccountConfigurationError(str(exc)) from None
            except ValueError as exc:
                raise MarketplaceAccountValidationError(str(exc)) from None
            account.is_active = True
            account.connection_status = 'unchecked'
            account.connection_checked_at = None
            account.credential_expires_at = None
            account.provider_request_id = None
            account.last_error_code = None
            account.last_error_message = None
            account.capabilities_json = '[]'
            account.roles_json = '[]'
            account.version = (account.version or 0) + 1
            append_event(account, before, 'key_replaced', actor_user_id)
            db.session.commit()
            return account
        except Exception:
            db.session.rollback()
            raise
        finally:
            release_account_operation_lock(claim)

    @classmethod
    def _save_normalized_ozon_account(
        cls,
        *,
        seller_id: int,
        marketplace: Marketplace,
        account: Optional[SellerMarketplaceAccount],
        external_account_id: str,
        label: str,
        normalized_api_key: Optional[str],
        is_default: bool,
        default_vat: Optional[str],
        actor_user_id: Optional[int] = None,
    ) -> SellerMarketplaceAccount:
        if account is None:
            existing_count = SellerMarketplaceAccount.query.filter_by(
                seller_id=seller_id,
                marketplace_id=marketplace.id,
            ).count()
            if existing_count >= cls.MAX_ACCOUNTS_PER_MARKETPLACE:
                raise MarketplaceAccountConflict(
                    "Достигнут лимит подключений Ozon для продавца"
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

        before = {} if is_new else account_snapshot(account)
        identity_changed = account.external_account_id != external_account_id
        credential_changed = normalized_api_key is not None
        account.external_account_id = external_account_id
        account.label = label
        account.is_active = True
        if default_vat is not None:
            try:
                settings = json.loads(account.settings_json or "{}")
            except (TypeError, json.JSONDecodeError):
                settings = {}
            if not isinstance(settings, dict):
                settings = {}
            settings["default_vat"] = default_vat
            account.settings_json = json.dumps(
                settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
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
            for previous in other_default.all():
                previous_before = account_snapshot(previous)
                previous.is_default = False
                previous.version += 1
                append_event(previous, previous_before, 'default_changed', actor_user_id)
            db.session.flush()
            account.is_default = True

        account.version = 1 if is_new else (account.version or 0) + 1
        try:
            db.session.flush()
            append_event(account, before, 'connected' if is_new else 'key_replaced' if credential_changed else 'settings_changed', actor_user_id)
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
        expected_version: Optional[int] = None,
    ) -> Tuple[SellerMarketplaceAccount, ConnectionCheck]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        cls.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
        )
        claim = try_account_operation_lock(account_id)
        if claim is None:
            raise MarketplaceAccountConflict(
                "Кабинет используется публикацией или сверкой; повторите позже"
            )
        try:
            db.session.expire_all()
            account = cls.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
            )
            if expected_version is not None and account.version != expected_version:
                raise MarketplaceAccountConflict(
                    "Настройки кабинета изменились; повторите проверку"
                )
            # This is a read-only roles check under the same physical account
            # claim as writes. Durable uncertain rows must not prevent key
            # recovery: their reconciliation depends on a working connection.
            return cls._check_connection_owned(
                account=account,
                registry=registry,
                now=now,
            )
        finally:
            release_account_operation_lock(claim)

    @classmethod
    def _check_connection_owned(
        cls,
        *,
        account: SellerMarketplaceAccount,
        registry=None,
        now: Optional[datetime] = None,
    ) -> Tuple[SellerMarketplaceAccount, ConnectionCheck]:
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
    def set_default(cls, *, seller_id, account_id, expected_version=None,
                    expected_default=None, actor_user_id=None):
        cls._require_clean_session()
        current = cls.get_owned_account(seller_id=seller_id, account_id=account_id)
        with cls._group_claim(current.seller_id, current.marketplace_id):
            account = cls.get_owned_account(seller_id=seller_id, account_id=account_id)
            validate_actor(seller_id, actor_user_id)
            if expected_version is not None:
                cls._reviewed_version(expected_version, account)
            if not account.is_active or not account.has_credentials:
                raise MarketplaceAccountConflict('Отключённый кабинет нельзя сделать основным')
            previous = SellerMarketplaceAccount.query.filter_by(seller_id=account.seller_id,
                marketplace_id=account.marketplace_id, is_default=True).one_or_none()
            if expected_default is not None:
                observed = {'id': previous.id if previous else None, 'version': previous.version if previous else None}
                if (not isinstance(expected_default, dict) or set(expected_default) != {'id', 'version'}
                        or any(value is not None and (type(value) is not int or value <= 0) for value in expected_default.values())):
                    raise MarketplaceAccountValidationError('Не удалось подтвердить просмотренный основной магазин.')
                if expected_default != observed:
                    raise MarketplaceAccountVersionConflict('Основной магазин изменился. Обновите страницу перед выбором.')
            if account.is_default:
                return account
            before = account_snapshot(account)
            if previous is not None:
                previous_before = account_snapshot(previous)
                previous.is_default = False
                previous.version += 1
                append_event(previous, previous_before, 'default_changed', actor_user_id)
                db.session.flush()
            account.is_default = True
            account.version += 1
            append_event(account, before, 'default_changed', actor_user_id)
            db.session.commit()
            return account

    @classmethod
    def disconnect(cls, *, seller_id, account_id, expected_version=None, actor_user_id=None):
        cls._require_clean_session()
        account = cls.get_owned_account(seller_id=seller_id, account_id=account_id)
        with cls._group_claim(account.seller_id, account.marketplace_id):
            return cls._disconnect_locked(seller_id=seller_id, account_id=account_id,
                expected_version=expected_version, actor_user_id=actor_user_id)

    @classmethod
    def _disconnect_locked(
        cls,
        *,
        seller_id: int,
        account_id: int,
        expected_version=None,
        actor_user_id=None,
    ) -> SellerMarketplaceAccount:
        account = cls.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
        )
        db.session.expire_all()
        account = cls.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
        )
        validate_actor(seller_id, actor_user_id)
        if expected_version is not None:
            cls._reviewed_version(expected_version, account)
        blocking = MarketplaceOperation.query.filter(
            MarketplaceOperation.seller_id == seller_id,
            MarketplaceOperation.account_id == account.id,
            MarketplaceOperation.status.in_(
                cls.RECONCILIATION_REQUIRED_STATUSES
            ),
        ).first()
        unsafe_queued = MarketplaceOperation.query.filter_by(
            seller_id=seller_id,
            account_id=account.id,
            status="queued",
        ).filter(
            MarketplaceOperation.attempt_count > 0,
        ).first()
        blocking_media = MarketplaceMediaOperation.query.filter(
            MarketplaceMediaOperation.seller_id == seller_id,
            MarketplaceMediaOperation.account_id == account.id,
            MarketplaceMediaOperation.status.in_((
                "preflighting",
                "submitting",
                "reconciling",
                "uncertain",
            )),
        ).first()
        unsafe_queued_media = MarketplaceMediaOperation.query.filter_by(
            seller_id=seller_id,
            account_id=account.id,
            status="queued",
        ).filter(
            MarketplaceMediaOperation.attempt_count > 0,
        ).first()
        if (
            blocking is not None
            or unsafe_queued is not None
            or blocking_media is not None
            or unsafe_queued_media is not None
        ):
            raise MarketplaceAccountConflict(
                "API key нельзя удалить, пока Ozon write требует сверки"
            )

        now = datetime.utcnow()
        queued = MarketplaceOperation.query.filter_by(
            seller_id=seller_id,
            account_id=account.id,
            status="queued",
            attempt_count=0,
        ).all()
        for operation in queued:
            operation.status = "cancelled"
            operation.error_code = "account_disconnected_before_submission"
            operation.error_message = (
                "Операция отменена до Ozon write при отключении кабинета"
            )
            operation.quota_reserved = 0
            operation.next_poll_at = None
            operation.completed_at = now

        queued_media = MarketplaceMediaOperation.query.filter_by(
            seller_id=seller_id,
            account_id=account.id,
            status="queued",
            attempt_count=0,
        ).all()
        media_publication_ids = set()
        for operation in queued_media:
            media_publication_ids.add(operation.publication_id)
            operation.status = "cancelled"
            operation.error_code = "account_disconnected_before_submission"
            operation.error_message = (
                "Media-операция отменена до Ozon write при отключении кабинета"
            )
            operation.next_reconcile_at = None
            operation.completed_at = now
        if media_publication_ids:
            db.session.flush()
            from services.marketplace_media_publications import refresh_publication
            for publication_id in media_publication_ids:
                publication = db.session.get(
                    MarketplaceMediaPublication, publication_id,
                )
                if publication is not None:
                    refresh_publication(publication, commit=False)

        proposals = MarketplaceCommercialProposal.query.filter(
            MarketplaceCommercialProposal.seller_id == seller_id,
            MarketplaceCommercialProposal.account_id == account.id,
            MarketplaceCommercialProposal.status.in_((
                "pending_review",
                "approved",
            )),
        ).all()
        for proposal in proposals:
            proposal.status = "cancelled"
            proposal.error_code = "account_disconnected_before_submission"
            proposal.error_message = (
                "Proposal отменён до Ozon write при отключении кабинета"
            )

        canonical_content_proposals = (
            MarketplaceCanonicalContentProposal.query.filter_by(
                seller_id=seller_id,
                account_id=account.id,
                status="pending_review",
            ).all()
        )
        for proposal in canonical_content_proposals:
            proposal.status = "conflict"
            proposal.error_code = "account_disconnected_before_review"
            proposal.error_message = (
                "Кабинет отключён до review; создайте новый diff после подключения"
            )

        return cls._disconnect_owned_account(account, actor_user_id=actor_user_id)

    @classmethod
    def _disconnect_owned_account(
        cls,
        account: SellerMarketplaceAccount,
        actor_user_id=None,
    ) -> SellerMarketplaceAccount:
        before = account_snapshot(account)
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
                replacement_before = account_snapshot(replacement)
                replacement.is_default = True
                replacement.version = (replacement.version or 0) + 1
                append_event(replacement, replacement_before, 'default_changed', actor_user_id)
        try:
            append_event(account, before, 'disconnected', actor_user_id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return account
