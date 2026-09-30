"""Route-level contracts for the unified marketplace listing workspace."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

from flask import Flask
from flask_login import LoginManager

from routes.marketplace_listings import register_marketplace_listing_routes


RETURN_URL = (
    "/marketplaces/listings/classic?marketplace=ozon&account_id=17&status=error"
    "&link_status=linked&include_unavailable=1&search=coat&page=2&per_page=25"
)


class _ProposalQuery:
    def filter_by(self, **_kwargs):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, _limit):
        return self

    def all(self):
        return []


class _ProposalModel:
    query = _ProposalQuery()
    created_at = None
    id = None


class _OrderColumn:
    def desc(self):
        return self


class ListingWorkspaceRouteTest(TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="ux01-listing-workspace-test",
            MARKETPLACE_OZON_ENABLED=False,
        )
        LoginManager(self.app)
        register_marketplace_listing_routes(self.app)
        self.client = self.app.test_client()
        self.seller_id = 23
        self.user = SimpleNamespace(
            id=23,
            seller=SimpleNamespace(id=self.seller_id),
            is_authenticated=True,
            is_active=True,
        )
        _ProposalModel.created_at = _OrderColumn()
        _ProposalModel.id = _OrderColumn()
        self.listing = SimpleNamespace(
            id=7,
            marketplace=SimpleNamespace(code="ozon"),
            account_id=17,
            title="Synthetic listing",
            offer_id="offer-7",
            external_product_id="ozon-7007",
            primary_sku="sku-7",
            normalized_status="error",
            is_available=True,
            visibility="visible",
            provider_status="failed",
            moderation_errors=[],
            canonical_link_status="unlinked",
            link_version=4,
            to_public_dict=lambda **_kwargs: {
                "id": 7,
                "marketplace_code": "ozon",
                "moderation_errors": [],
            },
        )

    def _auth(self):
        return (
            patch("routes.marketplace_listings.current_user", self.user),
            patch("flask_login.utils._get_user", return_value=self.user),
        )

    def _capture_template(self):
        captured = {}

        def render(template, **context):
            captured["template"] = template
            captured.update(context)
            return "rendered"

        return captured, patch(
            "routes.marketplace_listings.render_template",
            side_effect=render,
        )

    def test_beta_overview_and_classic_management_keep_exact_safe_return_target(self):
        captured, render_patch = self._capture_template()
        user_patch, login_patch = self._auth()
        with (
            user_patch,
            login_patch,
            render_patch,
            patch(
                "routes.marketplace_listings.MarketplaceListingService.get_listing",
                return_value=self.listing,
            ),
            patch(
                "routes.marketplace_listings.MarketplaceListingService.group_members",
                return_value=[{"id": 7, "marketplace_code": "ozon"}],
            ),
            patch(
                "routes.marketplace_listings.MarketplaceProductLinkService.context",
                return_value={},
            ),
            patch(
                "routes.marketplace_listings.MarketplaceWarehouseService.list_listing_stocks",
                return_value=[],
            ),
            patch(
                "routes.marketplace_listings.listing_display",
                return_value={},
            ),
            patch(
                "routes.marketplace_listings.MarketplaceCommercialProposal",
                _ProposalModel,
            ),
        ):
            overview = self.client.get(
                "/marketplaces/listings/beta/7?return_to="
                + quote(RETURN_URL, safe=""),
                headers={"Accept": "text/html"},
            )
            self.assertEqual(overview.status_code, 200)
            self.assertEqual(captured["return_url"], RETURN_URL)

            managed = self.client.get(
                "/marketplaces/listings/7?return_to="
                + quote(RETURN_URL, safe=""),
                headers={"Accept": "text/html"},
            )
            self.assertEqual(managed.status_code, 200)
            nav = captured["workspace_navigation"]
            self.assertEqual(nav["return_url"], RETURN_URL)
            self.assertEqual(nav["mode"], "management")
            for key, path in (
                ("overview_url", "/marketplaces/listings/view/7"),
                ("management_url", "/marketplaces/listings/7"),
            ):
                split = urlsplit(nav[key])
                self.assertEqual(split.path, path)
                self.assertEqual(parse_qs(split.query)["return_to"], [RETURN_URL])

    def test_invalid_or_duplicate_return_target_falls_back_to_listing_account(self):
        captured, render_patch = self._capture_template()
        user_patch, login_patch = self._auth()
        with (
            user_patch,
            login_patch,
            render_patch,
            patch(
                "routes.marketplace_listings.MarketplaceListingService.get_listing",
                return_value=self.listing,
            ),
            patch(
                "routes.marketplace_listings.MarketplaceListingService.group_members",
                return_value=[],
            ),
        ):
            response = self.client.get(
                "/marketplaces/listings/beta/7?return_to=https%3A%2F%2Fevil.test%2F"
                "&return_to=%2F%2Fevil.test%2F",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            captured["return_url"],
            "/marketplaces/listings/?marketplace=ozon&account_id=17",
        )

    def test_vue_bootstrap_uses_supported_page_and_per_page_values(self):
        captured, render_patch = self._capture_template()
        user_patch, login_patch = self._auth()
        with (
            user_patch,
            login_patch,
            render_patch,
            patch(
                "routes.marketplace_listings.MarketplaceAccountService.list_accounts",
                return_value=[],
            ),
            patch(
                "routes.marketplace_listings.MarketplaceListingService.latest_syncs",
                return_value={},
            ),
            patch(
                "routes.marketplace_listings.latest_account_jobs",
                return_value={},
            ),
        ):
            response = self.client.get(
                "/marketplaces/listings/beta?marketplace=ozon&page=-1&per_page=500",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["initial_filters"]["page"], 1)
        self.assertEqual(captured["initial_filters"]["per_page"], 60)
        self.assertEqual(captured["catalog_page"], 1)
        self.assertEqual(captured["catalog_per_page"], 60)
        self.assertEqual(
            captured["catalog_return_url"],
            "/marketplaces/listings/beta?marketplace=ozon&page=1&per_page=60",
        )

    def test_vue_bootstrap_keeps_valid_initial_page_and_capped_per_page(self):
        captured, render_patch = self._capture_template()
        user_patch, login_patch = self._auth()
        with (
            user_patch,
            login_patch,
            render_patch,
            patch(
                "routes.marketplace_listings.MarketplaceAccountService.list_accounts",
                return_value=[],
            ),
            patch(
                "routes.marketplace_listings.MarketplaceListingService.latest_syncs",
                return_value={},
            ),
            patch(
                "routes.marketplace_listings.latest_account_jobs",
                return_value={},
            ),
        ):
            response = self.client.get(
                "/marketplaces/listings/?marketplace=ozon&page=3&per_page=100",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["catalog_page"], 3)
        self.assertEqual(captured["catalog_per_page"], 100)
        self.assertEqual(
            captured["catalog_return_url"],
            "/marketplaces/listings/?marketplace=ozon&page=3&per_page=100",
        )
