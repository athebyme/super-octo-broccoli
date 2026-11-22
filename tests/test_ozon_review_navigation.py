"""Signed Ozon review returns stay exact, local, and actor scoped."""

import math
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from flask import Flask
from sqlalchemy import event

from models import db
from routes.marketplace_drafts import register_marketplace_draft_routes
from routes.ozon_bulk_uploads import register_ozon_bulk_upload_routes
from services.ozon_review_navigation import OzonReviewNavigation
from tests.test_marketplace_publications import OzonPublicationFixture


def review_document(
    draft_ids,
    *,
    account_id=41,
    page=1,
    parent_uid=None,
    result_uid=None,
):
    page_count = math.ceil(len(draft_ids) / OzonReviewNavigation.PAGE_SIZE)
    start = (page - 1) * OzonReviewNavigation.PAGE_SIZE
    page_ids = draft_ids[start:start + OzonReviewNavigation.PAGE_SIZE]
    return {
        "account_id": account_id,
        "draft_ids": list(draft_ids),
        "parent_prepare_job_uid": parent_uid,
        "result_job_uid": result_uid,
        "items": [{"draft_id": draft_id} for draft_id in page_ids],
        "pagination": {
            "page": page,
            "per_page": OzonReviewNavigation.PAGE_SIZE,
            "pages": page_count,
            "total": len(draft_ids),
        },
    }


class OzonReviewNavigationServiceTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "review-navigation-test-secret"
        self.app.add_url_rule(
            "/marketplaces/ozon/uploads/review",
            endpoint="ozon_bulk_uploads.review",
            view_func=lambda: "",
        )
        self.context = self.app.app_context()
        self.context.push()
        self.request_context = self.app.test_request_context("/")
        self.request_context.push()
        self.addCleanup(self.request_context.pop)
        self.addCleanup(self.context.pop)

    def issue(self, document, *, user_id=7, seller_id=9):
        return OzonReviewNavigation.issue_token(
            document,
            user_id=user_id,
            seller_id=seller_id,
        )

    def resolve(self, token, draft_id, **kwargs):
        return OzonReviewNavigation.resolve_navigation(
            token,
            user_id=kwargs.pop("user_id", 7),
            seller_id=kwargs.pop("seller_id", 9),
            draft_id=draft_id,
            draft_account_id=kwargs.pop("draft_account_id", 41),
            **kwargs,
        )

    def test_parent_scope_rebuilds_exact_ordered_page_url_and_focus_hint(self):
        ids = list(range(101, 128))
        parent = "ozon-upload-" + "a" * 32
        token = self.issue(review_document(ids, page=2, parent_uid=parent))

        navigation = self.resolve(
            token,
            ids[20],
            focus_code="physical_fact_required",
            focus_field="dimensions.weight",
        )

        self.assertEqual(
            navigation["focus"],
            {"code": "physical_fact_required", "field": "dimensions.weight"},
        )
        query = parse_qs(urlsplit(navigation["url"]).query)
        self.assertEqual(query["account_id"], ["41"])
        self.assertEqual(query["draft_ids"], [",".join(map(str, ids))])
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["parent_prepare_job_uid"], [parent])
        self.assertNotIn("result_job_uid", query)

    def test_result_scope_round_trips_without_a_parent_run(self):
        ids = list(range(201, 225))
        result = "ozon-upload-" + "b" * 32
        token = self.issue(review_document(ids, page=2, result_uid=result))

        navigation = self.resolve(token, ids[21])

        query = parse_qs(urlsplit(navigation["url"]).query)
        self.assertEqual(query["draft_ids"], [",".join(map(str, ids))])
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["result_job_uid"], [result])
        self.assertNotIn("parent_prepare_job_uid", query)

    def test_200_int64_ids_fit_the_bound_and_return_from_the_final_page(self):
        ids = [2**63 - index for index in range(1, 201)]
        token = self.issue(review_document(ids, page=10))

        self.assertIsNotNone(token)
        self.assertLessEqual(len(token), OzonReviewNavigation.MAX_TOKEN_CHARS)
        navigation = self.resolve(token, ids[-1])
        query = parse_qs(urlsplit(navigation["url"]).query)
        self.assertEqual(query["draft_ids"], [",".join(map(str, ids))])
        self.assertEqual(query["page"], ["10"])

    def test_same_account_draft_outside_the_signed_page_is_not_a_return_target(self):
        ids = list(range(1, 22))
        token = self.issue(review_document(ids, page=1))

        self.assertIsNone(self.resolve(token, ids[-1]))

    def test_actor_seller_account_and_tampered_tokens_fail_closed(self):
        token = self.issue(review_document([501]))
        changed = ("Z" if token[0] != "Z" else "Y") + token[1:]

        self.assertIsNone(self.resolve(token, 501, user_id=8))
        self.assertIsNone(self.resolve(token, 501, seller_id=10))
        self.assertIsNone(self.resolve(token, 501, draft_account_id=42))
        self.assertIsNone(self.resolve(changed, 501))

    def test_expired_token_fails_closed(self):
        document = review_document([601])
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=1):
            token = self.issue(document)

        self.assertIsNone(self.resolve(token, 601))

    def test_focus_is_optional_bounded_and_syntax_safe(self):
        token = self.issue(review_document([701]))

        safe_hint = self.resolve(
            token,
            701,
            focus_code="physical_fact_required",
            focus_field="content.name",
        )
        oversized = self.resolve(
            token,
            701,
            focus_code="x" * 81,
            focus_field="content.name",
        )
        unsafe = self.resolve(
            token,
            701,
            focus_code="bad code",
            focus_field="content.name",
        )

        self.assertEqual(
            safe_hint["focus"],
            {"code": "physical_fact_required", "field": "content.name"},
        )
        self.assertIsNone(oversized["focus"])
        self.assertIsNone(unsafe["focus"])

    def test_malformed_or_mutually_exclusive_scope_is_not_signed(self):
        ids = [801, 802]
        wrong_page = review_document(ids)
        wrong_page["items"] = [{"draft_id": ids[1]}]
        both_origins = review_document(
            ids,
            parent_uid="ozon-upload-" + "c" * 32,
            result_uid="ozon-upload-" + "d" * 32,
        )

        self.assertIsNone(self.issue(wrong_page))
        self.assertIsNone(self.issue(both_origins))

    def test_missing_secret_key_disables_token_issue_and_resolution(self):
        self.app.config["SECRET_KEY"] = None
        token = self.issue(review_document([901]))

        self.assertIsNone(token)
        self.assertIsNone(self.resolve("syntactically-valid.token", 901))


class OzonReviewNavigationRouteTest(OzonPublicationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(
            SECRET_KEY="review-navigation-route-test-secret",
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
        )
        register_marketplace_draft_routes(self.app)
        register_ozon_bulk_upload_routes(self.app)
        self.client = self.app.test_client()
        self.user_view = SimpleNamespace(
            id=self.user.id,
            seller=SimpleNamespace(id=self.seller.id),
            is_authenticated=True,
            is_active=True,
        )
        self.auth = patch("routes.marketplace_drafts.current_user", self.user_view)
        self.upload_auth = patch("routes.ozon_bulk_uploads.current_user", self.user_view)
        self.login = patch("flask_login.utils._get_user", return_value=self.user_view)
        self.auth.start()
        self.upload_auth.start()
        self.login.start()
        self.addCleanup(self.auth.stop)
        self.addCleanup(self.upload_auth.stop)
        self.addCleanup(self.login.stop)

    def _token(self, *, user_id=None, seller_id=None, account_id=None):
        document = review_document(
            [self.draft.id],
            account_id=account_id or self.account.id,
        )
        return OzonReviewNavigation.issue_token(
            document,
            user_id=user_id or self.user.id,
            seller_id=seller_id or self.seller.id,
        )

    def test_html_review_mints_token_but_json_review_is_unchanged(self):
        path = (
            f"/marketplaces/ozon/uploads/review?account_id={self.account.id}"
            f"&draft_ids={self.draft.id}"
        )
        with patch("routes.ozon_bulk_uploads.render_template", return_value="review") as render:
            html = self.client.get(path, headers={"Accept": "text/html"})
        self.assertEqual(html.status_code, 200)
        review = render.call_args.kwargs["review"]
        token = review.get("review_return_token")
        self.assertTrue(token)
        with self.app.test_request_context("/"):
            self.assertIsNotNone(OzonReviewNavigation.resolve_navigation(
                token,
                user_id=self.user.id,
                seller_id=self.seller.id,
                draft_id=self.draft.id,
                draft_account_id=self.account.id,
            ))

        json_response = self.client.get(
            path,
            headers={"Accept": "application/json"},
        )
        self.assertEqual(json_response.status_code, 200, json_response.get_json())
        self.assertNotIn("review_return_token", json_response.json["review"])

    def test_editor_passes_valid_return_and_bounded_focus_hint_to_vue_and_classic(self):
        token = self._token()
        issue = {"code": "physical_fact_required", "field": "dimensions.weight"}
        query = urlencode({
            "review_return": token,
            "focus_code": issue["code"],
            "focus_field": issue["field"],
        })
        writes = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                writes.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            with patch("requests.sessions.Session.request", side_effect=AssertionError("No provider I/O")):
                with patch("routes.marketplace_drafts.render_template", return_value="vue") as vue:
                    response = self.client.get(
                        f"/marketplaces/drafts/{self.draft.id}?{query}",
                        headers={"Accept": "text/html"},
                    )
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(writes, [])
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(vue.call_args.args[0], "marketplace_draft_detail.html")
        navigation = vue.call_args.kwargs["review_navigation"]
        self.assertEqual(
            navigation["focus"],
            {"code": issue["code"], "field": issue["field"]},
        )
        self.assertEqual(
            parse_qs(urlsplit(navigation["url"]).query),
            {
                "account_id": [str(self.account.id)],
                "draft_ids": [str(self.draft.id)],
                "page": ["1"],
            },
        )
        self.assertEqual(vue.call_args.kwargs["review_return_token"], token)

        with patch("routes.marketplace_drafts.render_template", return_value="classic") as classic:
            classic_response = self.client.get(
                f"/marketplaces/drafts/classic/{self.draft.id}?review_return={token}",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(classic_response.status_code, 200)
        self.assertIn("no-store", classic_response.headers.get("Cache-Control", ""))
        self.assertEqual(
            classic.call_args.args[0],
            "marketplace_draft_detail_classic.html",
        )
        self.assertEqual(classic.call_args.kwargs["review_navigation"]["focus"], None)
        self.assertEqual(classic.call_args.kwargs["review_return_token"], token)

    def test_invalid_scope_and_json_editor_keep_the_existing_fallback(self):
        wrong_actor_token = self._token(user_id=self.foreign_user.id)
        wrong_account_token = self._token(account_id=self.account.id + 1)
        with patch("routes.marketplace_drafts.render_template", return_value="vue") as render:
            invalid = self.client.get(
                f"/marketplaces/drafts/{self.draft.id}?review_return={wrong_actor_token}",
                headers={"Accept": "text/html"},
            )
            no_token = self.client.get(
                f"/marketplaces/drafts/{self.draft.id}",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(invalid.status_code, 200)
        self.assertIsNone(render.call_args.kwargs["review_navigation"])
        self.assertIsNone(render.call_args.kwargs["review_return_token"])
        self.assertEqual(no_token.status_code, 200)
        self.assertIsNone(render.call_args.kwargs["review_navigation"])
        self.assertIsNone(render.call_args.kwargs["review_return_token"])

        with patch("routes.marketplace_drafts.render_template", return_value="vue") as account_render:
            account_mismatch = self.client.get(
                f"/marketplaces/drafts/{self.draft.id}?review_return={wrong_account_token}",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(account_mismatch.status_code, 200)
        self.assertIsNone(account_render.call_args.kwargs["review_navigation"])

        json_response = self.client.get(
            f"/marketplaces/drafts/{self.draft.id}?review_return={self._token()}",
            headers={"Accept": "application/json"},
        )
        self.assertEqual(json_response.status_code, 200, json_response.get_json())
        self.assertNotIn("review_navigation", json_response.json)
        self.assertNotIn("review_return_token", json_response.json)

    def test_classic_local_post_preserves_one_bounded_return_token_only(self):
        token = self._token()
        first = self.client.post(
            f"/marketplaces/drafts/{self.draft.id}?view=classic&review_return={token}"
            "&focus_code=physical_fact_required&focus_field=dimensions.weight",
            data={
                "expected_version": str(self.draft.version),
                "content_json": '{"name":"Classic return one"}',
            },
        )
        self.assertEqual(first.status_code, 302, first.get_data(as_text=True))
        first_query = parse_qs(urlsplit(first.location).query)
        self.assertEqual(first_query.get("review_return"), [token])
        self.assertNotIn("focus_code", first_query)
        self.assertNotIn("focus_field", first_query)

        db.session.refresh(self.draft)
        duplicate = self.client.post(
            f"/marketplaces/drafts/{self.draft.id}?view=classic"
            "&review_return=first&review_return=second",
            data={
                "expected_version": str(self.draft.version),
                "content_json": '{"name":"Classic return duplicate"}',
            },
        )
        self.assertEqual(duplicate.status_code, 302)
        duplicate_query = parse_qs(urlsplit(duplicate.location).query)
        self.assertNotIn("review_return", duplicate_query)

        db.session.refresh(self.draft)
        oversized = self.client.post(
            f"/marketplaces/drafts/{self.draft.id}?view=classic&review_return="
            + ("x" * (OzonReviewNavigation.MAX_TOKEN_CHARS + 1)),
            data={
                "expected_version": str(self.draft.version),
                "content_json": '{"name":"Classic return oversized"}',
            },
        )
        self.assertEqual(oversized.status_code, 302)
        oversized_query = parse_qs(urlsplit(oversized.location).query)
        self.assertNotIn("review_return", oversized_query)


if __name__ == "__main__":
    unittest.main()
