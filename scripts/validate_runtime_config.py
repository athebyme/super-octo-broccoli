#!/usr/bin/env python3
"""Fail-fast validation for security-critical runtime configuration."""

from collections.abc import Mapping
import os
import sys

from cryptography.fernet import Fernet


class RuntimeConfigurationError(RuntimeError):
    """A required runtime invariant is not configured safely."""


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate_runtime_config(environment: Mapping[str, str]) -> None:
    """Validate config without returning or logging credential values."""
    if not _enabled(environment.get("MARKETPLACE_OZON_ENABLED")):
        return

    encryption_key = str(environment.get("ENCRYPTION_KEY") or "").strip()
    if not encryption_key:
        raise RuntimeConfigurationError(
            "ENCRYPTION_KEY is required when MARKETPLACE_OZON_ENABLED=1"
        )
    try:
        Fernet(encryption_key.encode("ascii"))
    except (TypeError, ValueError, UnicodeEncodeError):
        raise RuntimeConfigurationError(
            "ENCRYPTION_KEY must be a valid persistent Fernet key"
        ) from None


def main() -> int:
    try:
        validate_runtime_config(os.environ)
    except RuntimeConfigurationError as exc:
        print(f"Runtime configuration error: {exc}", file=sys.stderr)
        return 1
    print("Runtime configuration validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
