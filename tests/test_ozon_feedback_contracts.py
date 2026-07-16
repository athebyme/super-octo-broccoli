"""Strict Ozon reviews/questions contracts without network or ORM."""

from datetime import date, datetime

import pytest

from services.ozon_feedback_contracts import (
    OzonFeedbackContractError,
    build_question_list_request,
    build_review_list_request,
    normalize_question_list_response,
    normalize_review_list_response,
)


def _review(**overrides):
    row = {
        "id": "review-1",
        "sku": 101,
        "text": "Хороший товар",
        "rating": 5,
        "status": "NEW",
        "order_status": "DELIVERED",
        "published_at": "2026-07-15T10:30:00Z",
        "is_rating_participant": True,
        "comments_amount": 1,
        "photos_amount": 0,
        "videos_amount": 0,
        "author_name": "must-not-survive",
        "review_link": "https://must-not-survive.invalid",
    }
    row.update(overrides)
    return row


def _question(**overrides):
    row = {
        "id": "question-1",
        "sku": "101",
        "text": "Какой материал?",
        "status": "VIEWED",
        "published_at": "2026-07-14T09:00:00+03:00",
        "answers_count": 0,
        "author_name": "must-not-survive",
        "question_link": "https://must-not-survive.invalid",
        "product_url": "https://must-not-survive.invalid/product",
    }
    row.update(overrides)
    return row


def test_builders_keep_current_versions_distinct_and_bounded():
    review = build_review_list_request(
        status="NEW",
        date_from=date(2026, 4, 17),
        date_to="2026-07-15",
        limit=100,
        last_id="cursor-1",
    )
    question = build_question_list_request(
        status="PROCESSED",
        date_from="2026-07-01",
        date_to=date(2026, 7, 15),
        limit=25,
    )

    assert review == {
        "limit": 100,
        "sort_dir": "DESC",
        "last_id": "cursor-1",
        "filters": {
            "status": "NEW",
            "published_from": "2026-04-17T00:00:00Z",
            "published_to": "2026-07-15T23:59:59Z",
        },
    }
    assert question == {
        "limit": 25,
        "sort_dir": "DESC",
        "filter": {
            "status": "PROCESSED",
            "date_from": "2026-07-01T00:00:00Z",
            "date_to": "2026-07-15T23:59:59Z",
        },
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "UNKNOWN", "date_from": "2026-07-01", "date_to": "2026-07-15"},
        {"status": "new", "date_from": "2026-07-01", "date_to": "2026-07-15"},
        {"status": "NEW", "date_from": "2026-07-01", "date_to": "2026-07-15", "limit": True},
        {"status": "NEW", "date_from": "2026-07-01", "date_to": "2026-07-15", "limit": 101},
        {"status": "NEW", "date_from": "2026-07-15", "date_to": "2026-07-01"},
        {"status": "NEW", "date_from": "2026-01-01", "date_to": "2026-07-15"},
        {"status": "NEW", "date_from": "2026-7-1", "date_to": "2026-07-15"},
    ],
)
def test_builder_rejects_loose_or_unbounded_input(kwargs):
    with pytest.raises(OzonFeedbackContractError):
        build_review_list_request(**kwargs)


def test_review_normalization_is_pii_minimized_and_tracks_reply_eligibility():
    normalized = normalize_review_list_response(
        {
            "reviews": [
                _review(),
                _review(
                    id="review-2",
                    text=None,
                    photos_amount=0,
                    videos_amount=0,
                ),
                _review(
                    id="review-3",
                    text=None,
                    photos_amount=1,
                    videos_amount=0,
                ),
            ],
            "has_next": True,
            "last_id": "cursor-2",
        },
        requested_status="NEW",
        requested_last_id="cursor-1",
    )

    assert normalized["has_next"] is True
    assert normalized["next_last_id"] == "cursor-2"
    assert normalized["rows"][0]["published_at"] == datetime(2026, 7, 15, 10, 30)
    assert normalized["rows"][0]["sku"] == "101"
    assert normalized["rows"][0]["reply_eligible"] is True
    assert normalized["rows"][1]["reply_eligible"] is False
    assert normalized["rows"][2]["reply_eligible"] is True
    serialized_keys = set().union(*(row.keys() for row in normalized["rows"]))
    assert "author_name" not in serialized_keys
    assert "review_link" not in serialized_keys


def test_question_normalization_uses_utc_and_drops_provider_links_and_author():
    normalized = normalize_question_list_response(
        {"questions": [_question()], "has_next": False, "last_id": "ignored"},
        requested_status="VIEWED",
    )

    row = normalized["rows"][0]
    assert row["kind"] == "question"
    assert row["rating"] is None
    assert row["reply_eligible"] is True
    assert row["published_at"] == datetime(2026, 7, 14, 6, 0)
    assert "author_name" not in row
    assert "question_link" not in row
    assert "product_url" not in row
    assert normalized["next_last_id"] is None


@pytest.mark.parametrize(
    "response,requested_status,requested_last_id",
    [
        ({"reviews": [_review(status="VIEWED")], "has_next": False}, "NEW", None),
        ({"reviews": [_review(), _review()], "has_next": False}, "NEW", None),
        ({"reviews": [_review(rating=True)], "has_next": False}, "NEW", None),
        ({"reviews": [_review(published_at="2026-07-15T10:00:00")], "has_next": False}, "NEW", None),
        ({"reviews": [_review(published_at="2026-07-15 10:00:00Z")], "has_next": False}, "NEW", None),
        ({"reviews": [_review(published_at="2026-07-15T10:00:00+0300")], "has_next": False}, "NEW", None),
        ({"reviews": [], "has_next": True, "last_id": "next"}, "NEW", None),
        ({"reviews": [_review()], "has_next": True, "last_id": "same"}, "NEW", "same"),
        ({"reviews": [_review()], "has_next": "true", "last_id": "next"}, "NEW", None),
    ],
)
def test_normalizer_rejects_status_escape_duplicates_and_bad_cursor(
    response,
    requested_status,
    requested_last_id,
):
    with pytest.raises(OzonFeedbackContractError):
        normalize_review_list_response(
            response,
            requested_status=requested_status,
            requested_last_id=requested_last_id,
        )
