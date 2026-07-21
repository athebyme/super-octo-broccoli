"""Typed durable health state for automatic social publishing."""

from __future__ import annotations

from datetime import datetime


TERMINAL_PUBLISH_ERROR_CODES = frozenset({
    "vk_credentials_missing",
    "vk_auth_failed",
    "vk_user_token_required",
    "vk_permission_denied",
    "vk_access_denied",
    "vk_group_auth_unavailable",
})


def automatic_publish_is_blocked(account) -> bool:
    return bool(
        account.automatic_publish_blocked_at
        and account.last_error_code in TERMINAL_PUBLISH_ERROR_CODES
    )


def record_publish_failure(account, result, *, now: datetime) -> None:
    account.last_error = str(result.error or "Ошибка публикации")[:2000]
    account.last_error_code = (
        str(result.error_code)[:80] if result.error_code else None
    )
    account.last_error_at = now
    if (
        bool(result.terminal)
        and account.last_error_code in TERMINAL_PUBLISH_ERROR_CODES
    ):
        account.automatic_publish_blocked_at = now


def clear_publish_failure(account) -> None:
    account.last_error = None
    account.last_error_code = None
    account.last_error_at = None
    account.automatic_publish_blocked_at = None
