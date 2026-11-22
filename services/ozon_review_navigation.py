"""Short-lived, seller-scoped return context for Ozon review editing."""

from __future__ import annotations

import math
import re
from typing import Any, Optional

from flask import current_app, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from services.ozon_upload_review import OzonUploadReviewService


class OzonReviewNavigation:
    """Sign an exact review scope and rebuild its local return URL."""

    SALT = "ozon-review-navigation-v1"
    MAX_AGE_SECONDS = 30 * 60
    MAX_IDS = OzonUploadReviewService.MAX_ITEMS
    PAGE_SIZE = OzonUploadReviewService.PAGE_SIZE
    MAX_TOKEN_CHARS = 8_192
    _RUN_UID = re.compile(r"^ozon-upload-[0-9a-f]{32}$")
    _FOCUS_CODE = re.compile(r"^[A-Za-z0-9_.:\-\[\]]{1,80}$")
    _FOCUS_FIELD = re.compile(r"^[A-Za-z0-9_.:\-\[\]]{1,160}$")
    _TOKEN_SYNTAX = re.compile(r"^[A-Za-z0-9_.-]+$")

    @staticmethod
    def _positive(value: Any) -> bool:
        return type(value) is int and 0 < value <= 2**63 - 1

    @classmethod
    def _ordered_ids(cls, value: Any) -> Optional[list[int]]:
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= cls.MAX_IDS
            or any(not cls._positive(item) for item in value)
            or len(value) != len(set(value))
        ):
            return None
        return list(value)

    @classmethod
    def _document_payload(
        cls, review: Any, *, user_id: Any, seller_id: Any,
    ) -> Optional[dict]:
        if not isinstance(review, dict):
            return None
        account_id = review.get("account_id")
        draft_ids = cls._ordered_ids(review.get("draft_ids"))
        page = review.get("pagination", {}).get("page") if isinstance(
            review.get("pagination"), dict
        ) else None
        pagination = review.get("pagination")
        items = review.get("items")
        if (
            not cls._positive(user_id)
            or not cls._positive(seller_id)
            or not cls._positive(account_id)
            or draft_ids is None
            or type(page) is not int
            or not isinstance(pagination, dict)
            or type(pagination.get("pages")) is not int
            or pagination.get("pages") != math.ceil(len(draft_ids) / cls.PAGE_SIZE)
            or type(pagination.get("per_page")) is not int
            or pagination.get("per_page") != cls.PAGE_SIZE
            or type(pagination.get("total")) is not int
            or pagination.get("total") != len(draft_ids)
            or not 1 <= page <= pagination["pages"]
            or not isinstance(items, list)
        ):
            return None
        start = (page - 1) * cls.PAGE_SIZE
        page_ids = draft_ids[start:start + cls.PAGE_SIZE]
        if (
            len(items) != len(page_ids)
            or [item.get("draft_id") if isinstance(item, dict) else None for item in items]
            != page_ids
        ):
            return None
        parent_uid = review.get("parent_prepare_job_uid")
        result_uid = review.get("result_job_uid")
        if (
            parent_uid is not None
            and (not isinstance(parent_uid, str) or not cls._RUN_UID.fullmatch(parent_uid))
        ) or (
            result_uid is not None
            and (not isinstance(result_uid, str) or not cls._RUN_UID.fullmatch(result_uid))
        ) or (parent_uid is not None and result_uid is not None):
            return None
        return {
            "v": 1,
            "user_id": user_id,
            "seller_id": seller_id,
            "account_id": account_id,
            "draft_ids": draft_ids,
            "page": page,
            "parent_prepare_job_uid": parent_uid,
            "result_job_uid": result_uid,
        }

    @classmethod
    def _serializer(cls) -> Optional[URLSafeTimedSerializer]:
        secret_key = current_app.secret_key
        if not isinstance(secret_key, (str, bytes)) or not secret_key:
            return None
        return URLSafeTimedSerializer(
            secret_key,
            salt=cls.SALT,
        )

    @classmethod
    def has_bounded_token_syntax(cls, value: Any) -> bool:
        """Cheap redirect guard; signature and scope are checked on the GET."""
        return (
            isinstance(value, str)
            and 0 < len(value) <= cls.MAX_TOKEN_CHARS
            and cls._TOKEN_SYNTAX.fullmatch(value) is not None
        )

    @classmethod
    def issue_token(
        cls, review: Any, *, user_id: Any, seller_id: Any,
    ) -> Optional[str]:
        """Return a bounded token only for a validated exact review page."""
        payload = cls._document_payload(
            review, user_id=user_id, seller_id=seller_id,
        )
        if payload is None:
            return None
        serializer = cls._serializer()
        if serializer is None:
            return None
        token = serializer.dumps(payload)
        return token if len(token) <= cls.MAX_TOKEN_CHARS else None

    @classmethod
    def _load_payload(
        cls, token: Any, *, user_id: Any, seller_id: Any,
    ) -> Optional[dict]:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > cls.MAX_TOKEN_CHARS
            or not cls._positive(user_id)
            or not cls._positive(seller_id)
        ):
            return None
        serializer = cls._serializer()
        if serializer is None:
            return None
        try:
            payload = serializer.loads(
                token,
                max_age=cls.MAX_AGE_SECONDS,
            )
        except (BadSignature, SignatureExpired, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or set(payload) != {
            "v", "user_id", "seller_id", "account_id", "draft_ids", "page",
            "parent_prepare_job_uid", "result_job_uid",
        }:
            return None
        ids = cls._ordered_ids(payload.get("draft_ids"))
        page = payload.get("page")
        account_id = payload.get("account_id")
        parent_uid = payload.get("parent_prepare_job_uid")
        result_uid = payload.get("result_job_uid")
        if (
            payload.get("v") != 1
            or type(payload.get("v")) is not int
            or payload.get("user_id") != user_id
            or type(payload.get("user_id")) is not int
            or payload.get("seller_id") != seller_id
            or type(payload.get("seller_id")) is not int
            or not cls._positive(account_id)
            or ids is None
            or type(page) is not int
            or not 1 <= page <= math.ceil(len(ids) / cls.PAGE_SIZE)
            or (parent_uid is not None and (
                not isinstance(parent_uid, str) or not cls._RUN_UID.fullmatch(parent_uid)
            ))
            or (result_uid is not None and (
                not isinstance(result_uid, str) or not cls._RUN_UID.fullmatch(result_uid)
            ))
            or (parent_uid is not None and result_uid is not None)
        ):
            return None
        return payload

    @staticmethod
    def _bounded_focus_value(value: Any, *, maximum: int) -> Optional[str]:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            return None
        return value

    @classmethod
    def resolve_navigation(
        cls,
        token: Any,
        *,
        user_id: Any,
        seller_id: Any,
        draft_id: Any,
        draft_account_id: Any,
        focus_code: Any = None,
        focus_field: Any = None,
    ) -> Optional[dict]:
        """Resolve a valid token to a local review URL and optional focus hint."""
        payload = cls._load_payload(
            token, user_id=user_id, seller_id=seller_id,
        )
        if (
            payload is None
            or not cls._positive(draft_id)
            or not cls._positive(draft_account_id)
            or draft_account_id != payload["account_id"]
            or draft_id not in payload["draft_ids"][
                (payload["page"] - 1) * cls.PAGE_SIZE:
                payload["page"] * cls.PAGE_SIZE
            ]
        ):
            return None
        query = {
            "account_id": payload["account_id"],
            "draft_ids": ",".join(str(value) for value in payload["draft_ids"]),
            "page": payload["page"],
        }
        if payload["parent_prepare_job_uid"] is not None:
            query["parent_prepare_job_uid"] = payload["parent_prepare_job_uid"]
        if payload["result_job_uid"] is not None:
            query["result_job_uid"] = payload["result_job_uid"]
        focus = None
        code = cls._bounded_focus_value(focus_code, maximum=80)
        field = cls._bounded_focus_value(focus_field, maximum=160)
        if (
            code is not None
            and field is not None
            and cls._FOCUS_CODE.fullmatch(code)
            and cls._FOCUS_FIELD.fullmatch(field)
        ):
            focus = {"code": code, "field": field}
        return {
            "url": url_for("ozon_bulk_uploads.review", **query),
            "focus": focus,
        }
