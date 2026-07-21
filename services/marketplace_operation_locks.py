"""Process-shared locks for marketplace side-effect lifecycles."""

import fcntl
import os
import stat
import tempfile
from typing import Any, Optional


LOCK_DIRECTORY = os.path.join(
    tempfile.gettempdir(),
    "seller-hub-marketplace-publication-locks",
)


def _ensure_lock_directory() -> None:
    try:
        os.mkdir(LOCK_DIRECTORY, mode=0o700)
    except FileExistsError:
        metadata = os.lstat(LOCK_DIRECTORY)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RuntimeError(
                "Marketplace operation lock directory is not private"
            )


def _positive_integer(value: Any, field_name: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _try_operation_lock(scope: str, scope_id: int) -> Optional[Any]:
    _ensure_lock_directory()
    path = os.path.join(LOCK_DIRECTORY, f"{scope}-{scope_id}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise RuntimeError("Marketplace operation lock file is unsafe")
    os.fchmod(descriptor, 0o600)
    lock_file = os.fdopen(descriptor, "a+", encoding="ascii")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def try_account_operation_lock(account_id: Any) -> Optional[Any]:
    return _try_operation_lock(
        "account",
        _positive_integer(account_id, "account_id"),
    )


def try_wb_seller_media_lock(seller_id: Any) -> Optional[Any]:
    """Serialize every WB photo/gallery write for one seller on this host."""
    return _try_operation_lock(
        "wb-media-seller",
        _positive_integer(seller_id, "seller_id"),
    )


def release_marketplace_operation_lock(lock_file: Any) -> None:
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def release_account_operation_lock(lock_file: Any) -> None:
    release_marketplace_operation_lock(lock_file)


def release_wb_seller_media_lock(lock_file: Any) -> None:
    release_marketplace_operation_lock(lock_file)
