from cryptography.fernet import Fernet
import pytest

from scripts.validate_runtime_config import (
    RuntimeConfigurationError,
    validate_runtime_config,
)


def test_ozon_disabled_does_not_require_encryption_key():
    validate_runtime_config({"MARKETPLACE_OZON_ENABLED": "0"})


def test_ozon_enabled_requires_encryption_key():
    with pytest.raises(RuntimeConfigurationError, match="ENCRYPTION_KEY is required"):
        validate_runtime_config({"MARKETPLACE_OZON_ENABLED": "1"})


def test_ozon_enabled_rejects_invalid_key_without_echoing_it():
    secret = "not-a-valid-fernet-key"
    with pytest.raises(RuntimeConfigurationError) as error:
        validate_runtime_config({
            "MARKETPLACE_OZON_ENABLED": "true",
            "ENCRYPTION_KEY": secret,
        })

    assert secret not in str(error.value)


def test_ozon_enabled_accepts_persistent_fernet_key():
    validate_runtime_config({
        "MARKETPLACE_OZON_ENABLED": "yes",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    })
