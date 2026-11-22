"""Offline Playwright journey for the exact WB product edit flow.

Run with the repository virtualenv:
  python tests/ux01/wb_edit_browser.py

The fixture uses a disposable SQLite database, one synthetic WB account and a
fake provider boundary. Browser traffic is limited to loopback and the pinned
asset manifest. A sandbox that denies loopback listener creation writes a
machine-readable ``status=blocked`` report; it never claims browser success.
"""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="ux01-wb-edit-")
TEMP_PATH = Path(TEMP.name)
REPORT_PATH = Path(
    os.environ.get("UX01_WB_EDIT_REPORT")
    or os.environ.get("WB_EDIT_BROWSER_REPORT")
    or "/tmp/wb-edit-browser-report.json"
)
ARTIFACTS_PATH = Path(
    os.environ.get("UX01_WB_EDIT_ARTIFACTS")
    or REPORT_PATH.parent / "wb-edit-browser-artifacts"
)
SOURCE = os.environ.get("UX01_WB_EDIT_SOURCE", "worktree")
BULK_SCHEMA_RACE_ONLY = "--bulk-schema-race-only" in sys.argv
ASSET_DIR = ROOT / "tests/ozon_release/assets"
USERNAME = "ux01-wb-edit-seller"
PASSWORD = "synthetic-wb-edit-password"
FIXTURE = {}
SERVER = None
SERVER_THREAD = None
BASE = None

REPORT = {
    "source": SOURCE,
    "status": "running",
    "scope": "synthetic_wb_exact_selection_review_and_edit",
    "database": "disposable_sqlite",
    "network_policy": "loopback_and_hash_pinned_assets_only",
    "layouts": [],
    "footer_mobile_layouts": [],
    "bulk_review_mobile_layouts": [],
    "overflow_diagnostics": [],
    "checks": [],
    "interactions": [],
    "artifacts": [],
    "artifact_errors": [],
    "failure_context": None,
    "javascript_errors": [],
    "browser_mutations": [],
    "unexpected_external_requests": [],
    "unexpected_http": [],
    "expected_negative_http": [],
    "provider_attempts": 0,
    "fake_wb_write_calls": 0,
    "fake_wb_written_products": [],
    "fake_wb_client_instances": 0,
    "review_exclusion_observations": None,
    "fake_wb_single_write_calls": 0,
    "fake_wb_single_write_requests": [],
    "single_edit_contrast_diagnostic": [],
    "single_edit_footer_clearance": [],
    "single_edit_boundary_attempts": [],
    "single_edit_observations": {
        "form_post": None,
        "reopen": None,
        "progressive_ui": None,
        "rejections": [],
        "seller_scope_denials": [],
        "no_profile_denial": None,
    },
    "mixed_fixture_observations": None,
    "blocked_reason": None,
}
EXPECTED_NEGATIVE_HTTP = {
    ("POST", "/products/selection/resolve"): {
        "status": 403,
        "name": "foreign_product_selection_rejected",
    },
    ("GET", "/api/characteristics/Unmapped%20synthetic%20category"): {
        "status": 409,
        "name": "unmapped_category_rejected",
    },
}
OBSERVED_EXPECTED_NEGATIVE_HTTP = set()
EMPTY_PRODUCT_FORM_FILTERS = frozenset({
    "category", "has_stock", "block_status", "rating_min", "rating_max",
})

# Keep provider state separate from Product rows. The real single-card route
# reads the complete WB card, merges the submitted patch, and stores the
# provider readback; the synthetic client follows that same boundary.
_FAKE_WB_CARDS: dict[int, dict[str, object]] = {}

os.environ.update({
    "DATABASE_URL": "sqlite:///" + str(TEMP_PATH / "wb-edit.sqlite"),
    "SKIP_SCHEDULER": "1",
    "SECRET_KEY": "synthetic-wb-edit-browser-secret",
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"w" * 32).decode(),
    "MARKETPLACE_OZON_ENABLED": "0",
    "MARKETPLACE_OZON_PUBLICATION_ENABLED": "0",
    "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED": "0",
})


def forbidden_python_network(*_args, **_kwargs):
    REPORT["provider_attempts"] += 1
    raise AssertionError("Python provider/network traffic is forbidden in this fixture")


requests.sessions.Session.request = forbidden_python_network
socket.create_connection = forbidden_python_network


def write_report() -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_pinned_assets() -> dict[str, dict[str, object]]:
    manifest = json.loads((ASSET_DIR / "manifest.json").read_text(encoding="utf-8"))
    assets = {}
    for url, row in manifest.items():
        payload = (ASSET_DIR / row["file"]).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != row["sha256"]:
            raise AssertionError(f"Pinned browser asset changed: {url}")
        assets[url] = {"payload": payload, "content_type": row["content_type"]}

    vue_url = "https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.prod.js"
    vue_payload = (ROOT / "static/vendor/vue-3.4.38.global.prod.js").read_bytes()
    assets[vue_url] = {
        "payload": vue_payload,
        "content_type": "application/javascript",
    }
    return assets


def seed_synthetic_wb(app) -> dict[str, int]:
    from models import (
        Marketplace,
        MarketplaceCategory,
        MarketplaceCategoryCharacteristic,
        MarketplaceDirectory,
        Product,
        Seller,
        User,
        db,
    )

    with app.app_context():
        # Use one observed UTC instant for every cached reference row. This
        # keeps the fixture fresh at seed time without a hard-coded expiry.
        fixture_now = datetime.utcnow()
        db.create_all()
        owner = User(
            username=USERNAME,
            email="ux01-wb-edit@example.test",
            is_active=True,
        )
        owner.set_password(PASSWORD)
        foreign_user = User(
            username="ux01-wb-edit-foreign",
            email="ux01-wb-edit-foreign@example.test",
            is_active=True,
        )
        foreign_user.set_password(PASSWORD)
        no_profile_user = User(
            username="ux01-wb-edit-no-profile",
            email="ux01-wb-edit-no-profile@example.test",
            is_active=True,
        )
        no_profile_user.set_password(PASSWORD)
        mixed_user = User(
            username="ux01-wb-edit-mixed",
            email="ux01-wb-edit-mixed@example.test",
            is_active=True,
        )
        mixed_user.set_password(PASSWORD)
        db.session.add_all([owner, foreign_user, no_profile_user, mixed_user])
        db.session.flush()

        seller = Seller(
            user_id=owner.id,
            company_name="WB edit synthetic shop",
            wb_seller_id="wb-edit-account-synthetic",
        )
        seller.wb_api_key = "synthetic-provider-key-never-sent"
        foreign_seller = Seller(
            user_id=foreign_user.id,
            company_name="Foreign synthetic shop",
            wb_seller_id="foreign-wb-account-synthetic",
        )
        mixed_seller = Seller(
            user_id=mixed_user.id,
            company_name="Mixed eligibility synthetic shop",
            wb_seller_id="mixed-wb-account-synthetic",
        )
        mixed_seller.wb_api_key = "synthetic-mixed-provider-key-never-sent"
        db.session.add_all([seller, foreign_seller, mixed_seller])
        db.session.flush()

        marketplace = Marketplace(
            name="Wildberries",
            code="wb",
            adapter_code="wb",
            is_active=True,
            categories_sync_status="success",
            categories_synced_at=fixture_now,
        )
        db.session.add(marketplace)
        db.session.flush()
        category = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=5880,
            subject_name="Свечи эротик",
            is_enabled=True,
            is_leaf=True,
            is_available=True,
            characteristics_synced_at=fixture_now,
            characteristics_sync_status="success",
            characteristics_schema_hash="a" * 64,
            characteristics_version=1,
            characteristics_count=31,
        )
        db.session.add(category)
        db.session.flush()

        male_category = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=5070,
            subject_name="Мастурбаторы мужские",
            is_enabled=True,
            is_leaf=True,
            is_available=True,
            characteristics_synced_at=fixture_now,
            characteristics_sync_status="success",
            characteristics_schema_hash="b" * 64,
            characteristics_version=1,
            characteristics_count=31,
        )
        db.session.add(male_category)
        db.session.flush()

        definitions = [
            (101, "Synthetic free-text field", 1, 1, None, "[]"),
            (202, "Страна производства", 1, 1, None, "[]"),
            (303, "Вес товара", 4, 1, "г", "[]"),
            (404, "Материал", 1, 3, None, '["Силикон", "Пластик", "Металл"]'),
        ]
        definitions.extend(
            (
                500 + index,
                f"Synthetic field {index + 1}",
                2 if index == 1 else 1,
                3 if index == 2 else 1,
                None,
                "[]",
            )
            for index in range(28)
            if index != 1
        )
        db.session.add_all([
            MarketplaceCategoryCharacteristic(
                category_id=category.id,
                marketplace_id=marketplace.id,
                charc_id=char_id,
                name=name,
                charc_type=char_type,
                required=char_id == 500,
                unit_name=unit,
                max_count=max_count,
                dictionary_json=dictionary,
                dictionary_source="none",
                is_enabled=True,
                is_available=True,
            )
            for char_id, name, char_type, max_count, unit, dictionary in definitions
        ])
        db.session.add_all([
            MarketplaceCategoryCharacteristic(
                category_id=male_category.id,
                marketplace_id=marketplace.id,
                charc_id=800 + index,
                name=f"Synthetic male subject field {index + 1}",
                charc_type=1,
                required=False,
                unit_name=None,
                max_count=1,
                dictionary_json="[]",
                dictionary_source="none",
                is_enabled=True,
                is_available=True,
            )
            for index in range(31)
        ])
        db.session.add(MarketplaceDirectory(
            marketplace_id=marketplace.id,
            directory_type="countries",
            data_json='["Россия", "Китай"]',
            synced_at=fixture_now,
            sync_status="success",
            items_count=2,
            version=1,
        ))

        products = []
        for index in range(51):
            local_id = 9741 + index
            products.append(Product(
                id=local_id,
                seller_id=seller.id,
                nm_id=900000 + index,
                vendor_code=f"PD-{index:03d}",
                title=f"Pipedream Synthetic Product {index:03d}",
                brand="Pipedream",
                object_name="Свечи эротик",
                subject_id=5880,
                characteristics_json=(
                    '[{"id":101,"name":"Synthetic free-text field",'
                    '"value":["Existing synthetic value"]},'
                    '{"id":303,"name":"Вес товара","value":125},'
                    '{"id":404,"name":"Материал","value":["Пластик","Металл"]},'
                    '{"id":502,"name":"Synthetic field 3",'
                    '"value":["Existing line one","Existing line two"]},'
                    '{"id":501,"name":"Stale prior-subject field",'
                    '"value":["Preserve read-only historical value"]},'
                    '{"id":506,"name":"Synthetic field 7","value":[]}]'
                    if index == 0 else "[]"
                ),
                sizes_json=json.dumps([{
                    "techSize": "ONE SIZE",
                    "skus": [f"SYNTHETIC-WB-SKU-{index:03d}"],
                    "chrtID": 700000 + index,
                }]),
                photos_json=json.dumps([1]) if index == 0 else "[]",
                last_sync=fixture_now if index == 0 else None,
                is_active=True,
            ))
        foreign = Product(
            id=10001,
            seller_id=foreign_seller.id,
            nm_id=990001,
            vendor_code="FOREIGN-SECRET-CODE",
            title="Foreign private Pipedream title",
            brand="Pipedream",
            object_name="Свечи эротик",
            subject_id=5880,
            characteristics_json="[]",
            sizes_json="[]",
            photos_json="[]",
            is_active=True,
        )
        unmapped = Product(
            id=10002,
            seller_id=seller.id,
            nm_id=990002,
            vendor_code="UNMAPPED-FIXTURE",
            title="Synthetic unmapped category",
            brand="Other fixture brand",
            object_name="Unmapped synthetic category",
            subject_id=None,
            characteristics_json=(
                '[{"id":101,"name":"Stale prior-subject field",'
                '"value":["Preserve read-only historical value"]}]'
            ),
            sizes_json="[]",
            photos_json="[]",
            is_active=True,
        )
        male_subject_product = Product(
            id=10003,
            seller_id=seller.id,
            nm_id=990003,
            vendor_code="MALE-SUBJECT-FIXTURE",
            title="Synthetic male subject card",
            brand="Other fixture brand",
            object_name="Мастурбаторы мужские",
            subject_id=5070,
            characteristics_json="[]",
            sizes_json="[]",
            photos_json="[]",
            is_active=True,
        )
        mixed_products = []
        for index in range(50):
            mixed_products.append(Product(
                id=20000 + index,
                seller_id=mixed_seller.id,
                # The products table stores nmID as NOT NULL and unique per
                # seller; distinct negative synthetic sentinels are treated as
                # "not linked" by the real preview validator.
                nm_id=(910000 + index if index < 2 else -(index - 1)),
                vendor_code=f"MIXED-{index:03d}",
                title=f"Mixed isolated product {index:03d}",
                brand="Mixed Initial Brand",
                object_name="Свечи эротик",
                subject_id=5880,
                characteristics_json="[]",
                sizes_json="[]",
                photos_json="[]",
                is_active=True,
            ))
        db.session.add_all([
            *products, foreign, unmapped, male_subject_product, *mixed_products,
        ])
        db.session.commit()
        return {
            "seller_id": int(seller.id),
            "foreign_product_id": int(foreign.id),
            "product_id": int(products[0].id),
            "product_count": len(products),
            "no_profile_username": no_profile_user.username,
            "mixed_username": mixed_user.username,
            "mixed_seller_id": int(mixed_seller.id),
            "mixed_product_ids": [int(product.id) for product in mixed_products],
            "mixed_changed_product_ids": [int(product.id) for product in mixed_products[:2]],
            "mixed_nm_ids": [int(product.nm_id) for product in mixed_products[:2]],
            "male_subject_product_id": int(male_subject_product.id),
            "unmapped_product_id": int(unmapped.id),
        }


class FakeWBClient:
    """Provider boundary fake; records simulated writes without HTTP."""

    _UPDATE_FIELDS = frozenset({
        "vendorCode", "title", "description", "brand", "characteristics",
    })

    @staticmethod
    def _copy_card(card: dict[str, object]) -> dict[str, object]:
        return json.loads(json.dumps(card, ensure_ascii=False))

    @classmethod
    def _read_full_card(cls, nm_id: int) -> dict[str, object]:
        """Read the fake provider's current full card, seeding once from fixture DB."""
        from models import Product

        nm_id = int(nm_id)
        if nm_id not in _FAKE_WB_CARDS:
            product = Product.query.filter_by(nm_id=nm_id).one()
            _FAKE_WB_CARDS[nm_id] = {
                "nmID": nm_id,
                "subjectID": int(product.subject_id or 0),
                "vendorCode": product.vendor_code or "",
                "title": product.title,
                "brand": product.brand,
                "description": product.description,
                "sizes": json.loads(product.sizes_json or "[]"),
                "characteristics": json.loads(product.characteristics_json or "[]"),
            }
        return cls._copy_card(_FAKE_WB_CARDS[nm_id])

    @staticmethod
    def _merge_characteristics(
        full_card: dict[str, object], patch: object,
    ) -> None:
        if not isinstance(patch, list):
            raise AssertionError("fake WB characteristic update must be a list")
        existing_rows = full_card.get("characteristics")
        if not isinstance(existing_rows, list):
            raise AssertionError("fake WB full-card characteristics must be a list")
        rows = [row for row in existing_rows]
        positions = {
            int(row["id"]): index
            for index, row in enumerate(rows)
            if isinstance(row, dict)
            and type(row.get("id")) is int
        }
        for item in patch:
            if not isinstance(item, dict) or type(item.get("id")) is not int:
                raise AssertionError("fake WB characteristic patch needs an exact integer ID")
            char_id = int(item["id"])
            prior_index = positions.get(char_id)
            prior = rows[prior_index] if prior_index is not None else {}
            updated = {
                **(prior if isinstance(prior, dict) else {}),
                "id": char_id,
                "name": item.get("name"),
                "value": item.get("value"),
            }
            if prior_index is None:
                positions[char_id] = len(rows)
                rows.append(updated)
            else:
                rows[prior_index] = updated
        full_card["characteristics"] = rows

    @classmethod
    def _merge_updates(
        cls, full_card: dict[str, object], updates: dict[str, object],
    ) -> dict[str, object]:
        if not isinstance(updates, dict) or not updates:
            raise AssertionError("fake WB write needs a non-empty update mapping")
        unsupported = set(updates).difference(cls._UPDATE_FIELDS)
        if unsupported:
            raise AssertionError(
                f"fake WB client received unsupported update fields: {sorted(unsupported)}"
            )
        after = cls._copy_card(full_card)
        if "characteristics" in updates:
            cls._merge_characteristics(after, updates["characteristics"])
        for field in cls._UPDATE_FIELDS.difference({"characteristics"}):
            if field in updates:
                value = updates[field]
                if not isinstance(value, str):
                    raise AssertionError(f"fake WB scalar update {field} must be text")
                after[field] = value
        return after

    def __init__(self, *_args, **_kwargs):
        REPORT["fake_wb_client_instances"] += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def update_cards_merged(self, updates, *, pre_merge_callback=None, **_kwargs):
        REPORT["fake_wb_write_calls"] += 1
        sent = []
        snapshots = {}
        for nm_id, row in updates.items():
            nm_id = int(nm_id)
            before = self._read_full_card(nm_id)
            if pre_merge_callback:
                pre_merge_callback(nm_id, self._copy_card(before), self._copy_card(row))
            after = self._merge_updates(before, row)
            _FAKE_WB_CARDS[nm_id] = self._copy_card(after)
            snapshots[nm_id] = {
                "before": before,
                "after": self._copy_card(after),
            }
            sent.append(nm_id)
        REPORT["fake_wb_written_products"].extend(sent)
        return {"sent": sent, "missing": [], "invalid": {}, "failed": {}, "snapshots": snapshots}

    def update_card(
        self, nm_id, updates, *, snapshot_context, before_send_callback, **_kwargs,
    ):
        """Simulate full live read, exact patch merge, write and full readback."""
        nm_id = int(nm_id)
        before = self._read_full_card(nm_id)
        snapshot_context["before"] = self._copy_card(before)
        if before_send_callback:
            before_send_callback({"before": self._copy_card(before)})

        after = self._merge_updates(before, updates)
        _FAKE_WB_CARDS[nm_id] = self._copy_card(after)
        snapshot_context["after"] = self._copy_card(after)
        patch = updates.get("characteristics") or []
        core_fields = ("vendorCode", "title", "description", "brand")
        requested_fields = sorted(updates)
        core_fields_requested = sorted(set(requested_fields).intersection(core_fields))
        core_fields_changed = [
            field for field in core_fields
            if before.get(field) != after.get(field)
        ]
        REPORT["fake_wb_single_write_calls"] += 1
        REPORT["fake_wb_single_write_requests"].append({
            "nm_id": nm_id,
            "requested_fields": requested_fields,
            "core_fields_requested": core_fields_requested,
            "core_fields_changed": core_fields_changed,
            "fake_provider_core_before": {
                field: before.get(provider_field)
                for field, provider_field in (
                    ("vendor_code", "vendorCode"), ("title", "title"),
                    ("description", "description"), ("brand", "brand"),
                )
            },
            "fake_provider_core_after": {
                field: after.get(provider_field)
                for field, provider_field in (
                    ("vendor_code", "vendorCode"), ("title", "title"),
                    ("description", "description"), ("brand", "brand"),
                )
            },
            "characteristic_ids": [int(item["id"]) for item in patch],
            "characteristics": [
                {"id": int(item["id"]), "value": item["value"]}
                for item in patch
            ],
            "full_card_read_before": True,
            "full_card_patch_merged": True,
            "full_card_readback": True,
            "sizes_preserved_in_readback": after["sizes"] == before["sizes"],
            "sku_preserved_in_readback": after["sizes"] == before["sizes"],
        })


def local_post_allowlist(fixture: dict[str, int]) -> frozenset[str]:
    unmapped_product_id = fixture.get("unmapped_product_id")
    product_id = fixture.get("product_id")
    foreign_product_id = fixture.get("foreign_product_id")
    if (
        type(unmapped_product_id) is not int or unmapped_product_id <= 0
        or type(product_id) is not int or product_id <= 0
        or type(foreign_product_id) is not int or foreign_product_id <= 0
    ):
        raise ValueError("browser fixture needs exact positive product IDs")
    return frozenset({
        "/login",
        "/products/selection/resolve",
        "/api/characteristics/multi-category",
        "/products/bulk-edit",
        f"/products/{product_id}/edit",
        f"/products/{foreign_product_id}/edit",
        f"/products/{unmapped_product_id}/edit",
    })


def pinned_asset_for_url(url: str, assets: dict[str, dict[str, object]]):
    asset = assets.get(url)
    if asset is not None:
        return asset
    # Chromium resolves the existing pinned <script src="https://cdn.tailwindcss.com">
    # to the slash-terminated origin URL. Alias only that exact root URL.
    if url == "https://cdn.tailwindcss.com/":
        return assets.get("https://cdn.tailwindcss.com")
    return None


def bridge(
    route,
    *,
    assets: dict[str, dict[str, object]],
    origin: str,
    allowed_local_posts: frozenset[str],
) -> None:
    request = route.request
    parsed = urlsplit(request.url)
    if request.url.startswith(origin + "/"):
        if request.method not in {"GET", "HEAD", "POST"}:
            REPORT["unexpected_external_requests"].append({
                "method": request.method,
                "path": parsed.path,
                "reason": "unsupported_method",
            })
            route.abort()
            return
        if request.method == "POST" and parsed.path not in allowed_local_posts:
            REPORT["unexpected_external_requests"].append({
                "method": request.method,
                "path": parsed.path,
                "reason": "unapproved_local_mutation",
            })
            route.abort()
            return
        route.continue_()
        return
    asset = pinned_asset_for_url(request.url, assets)
    if asset:
        route.fulfill(
            status=200,
            body=asset["payload"],
            content_type=asset["content_type"],
        )
        return
    REPORT["unexpected_external_requests"].append({
        "method": request.method,
        "host": parsed.hostname,
        "path": parsed.path,
        "reason": "not_hash_pinned",
    })
    route.abort()


def record_http_response(response) -> None:
    parsed = urlsplit(response.url)
    if not BASE or f"{parsed.scheme}://{parsed.netloc}" != BASE:
        return
    if response.status < 400:
        return
    request_key = (response.request.method, parsed.path)
    expected = EXPECTED_NEGATIVE_HTTP.get(request_key)
    if expected and response.status == expected["status"]:
        if request_key in OBSERVED_EXPECTED_NEGATIVE_HTTP:
            REPORT["unexpected_http"].append({
                "status": response.status,
                "method": request_key[0],
                "path": request_key[1],
                "reason": "duplicate_expected_negative_probe",
            })
        else:
            OBSERVED_EXPECTED_NEGATIVE_HTTP.add(request_key)
            REPORT["expected_negative_http"].append({
                "name": expected["name"],
                "method": request_key[0],
                "path": request_key[1],
                "status": response.status,
            })
            check(expected["name"], method=request_key[0], status=response.status)
    else:
        REPORT["unexpected_http"].append({
            "status": response.status,
            "method": response.request.method,
            "path": parsed.path,
        })


def check(name: str, **details) -> None:
    REPORT["checks"].append({"name": name, "status": "passed", **details})


def interaction(name: str, **details) -> None:
    REPORT["interactions"].append({"name": name, **details})


def assert_single_edit_local_state(
    seller_app, fixture: dict[str, int], *, expected_history_count: int,
    expected_changed_fields: list[str] | None = None,
) -> dict[str, object]:
    """Read the disposable DB to prove writes and rejected posts preserve facts."""
    from models import CardEditHistory, Product

    expected_sizes = [{
        "techSize": "ONE SIZE",
        "skus": ["SYNTHETIC-WB-SKU-000"],
        "chrtID": 700000,
    }]
    expected_values = {
        101: ["Existing synthetic value"],
        202: ["Россия"],
        303: 125,
        404: ["Пластик", "Металл"],
        502: ["Existing line one", "Existing line two"],
        501: ["Preserve read-only historical value"],
        506: [],
    }
    with seller_app.app_context():
        product = Product.query.filter_by(
            id=fixture["product_id"], seller_id=fixture["seller_id"],
        ).one()
        observed_characteristics = json.loads(product.characteristics_json)
        by_id = {
            int(row["id"]): row.get("value")
            for row in observed_characteristics
            if isinstance(row, dict) and isinstance(row.get("id"), int)
        }
        assert by_id == expected_values, by_id
        assert json.loads(product.sizes_json) == expected_sizes
        assert product.nm_id == 900000
        assert product.vendor_code == "PD-000"
        assert product.title == "  Pipedream Synthetic Product 000  "
        assert product.brand is None
        assert product.description is None

        direct_history = CardEditHistory.query.filter_by(
            product_id=product.id,
            seller_id=fixture["seller_id"],
            bulk_edit_id=None,
            action="update",
        ).order_by(CardEditHistory.id.asc()).all()
        assert len(direct_history) == expected_history_count
        if expected_history_count:
            assert len(direct_history) == 1
            history = direct_history[0]
            assert history.changed_fields == expected_changed_fields
            assert history.wb_synced is True
            assert history.wb_sync_status == "success"
            before = history.snapshot_before["characteristics"]
            after = history.snapshot_after["characteristics"]
            before_by_id = {int(row["id"]): row.get("value") for row in before}
            after_by_id = {int(row["id"]): row.get("value") for row in after}
            assert before_by_id == {
                101: ["Existing synthetic value"],
                303: 125,
                404: ["Пластик", "Металл"],
                502: ["Existing line one", "Existing line two"],
                501: ["Preserve read-only historical value"],
                506: [],
            }
            assert after_by_id == expected_values
            assert history.snapshot_before["title"] == product.title
            assert history.snapshot_after["title"] == product.title
            assert history.snapshot_before["brand"] is None
            assert history.snapshot_after["brand"] is None
            assert history.snapshot_before["description"] is None
            assert history.snapshot_after["description"] is None
        return {
            "characteristic_ids": sorted(by_id),
            "size_count": len(expected_sizes),
            "sku": expected_sizes[0]["skus"][0],
            "local_core_values": {
                "vendor_code": product.vendor_code,
                "title": product.title,
                "description": product.description,
                "brand": product.brand,
            },
            "direct_history_count": len(direct_history),
            "history_changed_fields": (
                list(direct_history[0].changed_fields)
                if direct_history else []
            ),
            "history_before_core_values": ({
                field: direct_history[0].snapshot_before.get(field)
                for field in ("vendor_code", "title", "description", "brand")
            } if direct_history else None),
            "history_after_core_values": ({
                field: direct_history[0].snapshot_after.get(field)
                for field in ("vendor_code", "title", "description", "brand")
            } if direct_history else None),
        }


def post_from_isolated_session(page, *, path: str, value: str = "Россия") -> dict:
    """Send a CSRF-bearing form POST without sharing the primary browser session."""
    return page.evaluate("""async ({path, value}) => {
        const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
        const body = new URLSearchParams({csrf_token: csrf, char_202: value}).toString();
        const response = await fetch(path, {
            method: 'POST', credentials: 'same-origin', redirect: 'follow',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': csrf,
            },
            body,
        });
        return {
            status: response.status,
            path: new URL(response.url).pathname,
            redirected: response.redirected,
            body: await response.text(),
        };
    }""", {"path": path, "value": value})


def authenticate_isolated_browser_session(page, *, username: str, next_path: str) -> dict:
    """Authenticate a clean browser context through the real CSRF-protected login route."""
    next_query = "%2Fproducts" if next_path == "/products" else "%2Fdashboard"
    page.goto(BASE + f"/login?next={next_query}", wait_until="domcontentloaded")
    return page.evaluate("""async ({username, password, nextPath}) => {
        const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
        const body = new URLSearchParams({
            csrf_token: csrf, username, password,
        }).toString();
        const response = await fetch(`/login?next=${encodeURIComponent(nextPath)}`, {
            method: 'POST', credentials: 'same-origin', redirect: 'follow',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': csrf,
            },
            body,
        });
        return {
            status: response.status,
            path: new URL(response.url).pathname,
            redirected: response.redirected,
        };
    }""", {
        "username": username,
        "password": PASSWORD,
        "nextPath": next_path,
    })


def assert_bulk_editor_mobile_footer(
    page, expected_return_query: dict[str, list[str]],
) -> None:
    """Check the sticky action bar at narrow widths and its exact return path."""
    page.locator('input[name="operation"][value="update_brand"]').check()
    footer = page.locator('form[action="/products/bulk-edit"] > div.sticky.bottom-0')
    footer.wait_for(state="visible")
    cancel_link = footer.get_by_role("link", name="Отмена", exact=True)
    return_to = page.locator('form[action="/products/bulk-edit"] input[name="return_to"]')
    expected_return = return_to.input_value()
    assert cancel_link.get_attribute("href") == expected_return
    _assert_local_products_href(expected_return, expected_return_query)

    submit_label = footer.locator('button[type="submit"] span[x-text="submitLabel"]')
    original_label = submit_label.inner_text()
    long_label = (
        "Сформировать проверку, сверить выбранные изменения и продолжить "
        "для всех пятидесяти выбранных товаров"
    )
    submit_label.evaluate("(element, text) => { element.textContent = text; }", long_label)

    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
        for width in (320, 360, 390):
            page.set_viewport_size({"width": width, "height": 844})
            wait_for_layout_settle(page, width=width, theme=theme)
            measurement = page.evaluate("""() => {
                const footer=document.querySelector('form[action="/products/bulk-edit"] > div.sticky.bottom-0');
                const row=footer?.firstElementChild;
                const actions=row?.lastElementChild;
                const cancel=footer?.querySelector('a');
                const button=footer?.querySelector('button[type="submit"]');
                const label=button?.querySelector('[x-text="submitLabel"]');
                const rect=element => {
                    if(!element)return null;
                    const box=element.getBoundingClientRect();
                    return {left:box.left,right:box.right,width:box.width,height:box.height};
                };
                return {
                    viewport_width:window.innerWidth,
                    document_width:document.documentElement.scrollWidth,
                    body_width:document.body.scrollWidth,
                    footer:rect(footer),
                    cancel:rect(cancel),
                    submit:rect(button),
                    actions_direction:actions ? getComputedStyle(actions).flexDirection : null,
                    footer_row_direction:row ? getComputedStyle(row).flexDirection : null,
                    label_wraps_without_overflow:!!label && label.clientWidth > 0
                        && label.scrollWidth <= label.clientWidth + 1,
                };
            }""")
            footer_box = measurement["footer"] or {}
            cancel_box = measurement["cancel"] or {}
            submit_box = measurement["submit"] or {}
            assert measurement["viewport_width"] == width, measurement
            assert measurement["document_width"] <= width and measurement["body_width"] <= width, measurement
            assert footer_box.get("left", -1) >= -0.75 and footer_box.get("right", width + 1) <= width + 0.75, measurement
            assert measurement["footer_row_direction"] == "column", measurement
            assert measurement["actions_direction"] == "column", measurement
            assert cancel_box.get("width", 0) >= 44 and cancel_box.get("height", 0) >= 44, measurement
            assert submit_box.get("width", 0) >= 44 and submit_box.get("height", 0) >= 44, measurement
            assert cancel_box.get("left", -1) >= -0.75 and cancel_box.get("right", width + 1) <= width + 0.75, measurement
            assert submit_box.get("left", -1) >= -0.75 and submit_box.get("right", width + 1) <= width + 0.75, measurement
            assert measurement["label_wraps_without_overflow"] is True, measurement
            REPORT["footer_mobile_layouts"].append({
                "theme": theme,
                "viewport_width": width,
                "document_width": measurement["document_width"],
                "controls_in_viewport": True,
                "touch_targets_at_least_44px": True,
                "long_label_fits": True,
                "layout_direction": measurement["footer_row_direction"],
            })

    submit_label.evaluate("(element, text) => { element.textContent = text; }", original_label)
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
    page.set_viewport_size({"width": 1280, "height": 900})
    wait_for_layout_settle(page, width=1280, theme="light")
    with page.expect_navigation(wait_until="domcontentloaded"):
        cancel_link.click()
    _assert_local_products_url(page.url, expected_return_query)
    assert page.locator("#selectedCount").inner_text().strip() == "50"
    assert page.locator('select[name="sort"]').input_value() == "title"
    interaction("sticky_cancel_restores_exact_filter_sort_page_selection")
    check(
        "sticky_footer_mobile_long_label_and_exact_cancel_return",
        widths=[320, 360, 390],
        themes=["light", "dark"],
        layouts=len(REPORT["footer_mobile_layouts"]),
        minimum_touch_target_px=44,
        exact_return_context=True,
    )
    page.get_by_role("button", name="Редактировать", exact=True).click()
    page.wait_for_url("**/products/bulk-edit")


def assert_bulk_schema_load_races(page) -> dict[str, object]:
    """Drive deferred responses through the rendered Alpine bulk editor method."""
    page.wait_for_function("""() => {
        return !!window.Alpine?.reactive
            && typeof window.bulkEditFormData === 'function';
    }""", timeout=5000)
    return page.evaluate("""async () => {
        const source = window.bulkEditFormData();
        const originalLoader = source.loadCharacteristics;
        const state = window.Alpine.reactive(source);
        const originalFetch = window.fetch;
        const requests = [];
        const loads = [];
        const schemaPath = '/api/characteristics/multi-category';
        state.loadCharacteristics = function (...args) {
            const promise = originalLoader.apply(this, args);
            loads.push(promise);
            return promise;
        };
        window.fetch = (url, init) => {
            const requestUrl = new URL(
                typeof url === 'string' ? url : url.url,
                window.location.href,
            );
            if (
                requestUrl.origin !== window.location.origin
                || requestUrl.pathname !== schemaPath
                || String(init?.method || 'GET').toUpperCase() !== 'POST'
            ) {
                return originalFetch.call(window, url, init);
            }
            return new Promise(resolve => {
                requests.push({url: requestUrl.href, init, resolve});
            });
        };
        state.categories = [
            {subject_id: 100, name: 'Subject A'},
            {subject_id: 200, name: 'Subject B'},
        ];
        state.products = [{id: 1}];
        state.operation = 'update_characteristic';

        const schema = (id, name) => [{id, name, charc_type: 0, values: []}];
        function start(subjectId) {
            state.selectCategory(String(subjectId));
            const index = loads.length - 1;
            const request = requests[index];
            if (!request || !loads[index]) {
                throw new Error('Expected a deferred schema request for subject ' + subjectId);
            }
            const body = JSON.parse(request.init.body);
            if (body.subject_ids.join(',') !== String(subjectId)) {
                throw new Error('Wrong subject filter: ' + JSON.stringify(body.subject_ids));
            }
            return {request, promise: loads[index]};
        }
        async function settle(item, {status = 200, bySubject = {}, common = [], error = null} = {}) {
            item.request.resolve({
                ok: status >= 200 && status < 300,
                status,
                json: async () => ({
                    by_subject: bySubject,
                    common,
                    ...(error ? {error} : {}),
                }),
            });
            await item.promise;
        }
        function snapshot() {
            const copyRows = rows => rows.map(row => ({id: String(row.id), name: row.name}));
            return {
                category: state.selectedCategory,
                operation: state.operation,
                characteristics: copyRows(state.characteristics),
                common: copyRows(state.commonCharacteristics),
                byCategory: Object.fromEntries(Object.entries(state.characteristicsByCategory)
                    .map(([id, rows]) => [id, copyRows(rows)])),
                selectedChar: state.selectedChar
                    ? {id: String(state.selectedChar.id), name: state.selectedChar.name}
                    : null,
                selectedCharId: state.selectedCharId,
                charValue: state.charValue,
                charValueAdd: state.charValueAdd,
                selectedValues: [...state.selectedValues],
                selectedValuesAdd: [...state.selectedValuesAdd],
                pendingChanges: state.pendingChanges.map(change => ({...change})),
                loading: state.loading,
                loadError: state.loadError,
            };
        }
        function unchanged(actual, expected, label) {
            if (JSON.stringify(actual) !== JSON.stringify(expected)) {
                throw new Error(label + ': ' + JSON.stringify({expected, actual}));
            }
        }
        function pinInProgressBState() {
            state.selectedChar = {id: 'chosen', name: 'Chosen on B'};
            state.selectedCharId = 'chosen';
            state.charValue = 'B draft';
            state.charValueAdd = 'B add draft';
            state.selectedValues = ['b-value'];
            state.selectedValuesAdd = ['b-add-value'];
            state.pendingChanges = [{
                charId: '9', charName: 'Prepared B change',
                value: 'prepared', displayValue: 'prepared',
            }];
        }

        try {
            // Older A success after B has loaded must not replace B's schema or draft.
            const successA = start(100);
            const currentB = start(200);
            await settle(currentB, {bySubject: {'200': schema(9, 'B schema')}});
            pinInProgressBState();
            const afterB = snapshot();
            await settle(successA, {bySubject: {'100': schema(9, 'A schema')}});
            unchanged(snapshot(), afterB, 'stale A success changed current B editor state');

            // A stale rejection must not put an error on the successfully loaded B schema.
            const failureA = start(100);
            const secondB = start(200);
            await settle(secondB, {bySubject: {'200': schema(9, 'B schema')}});
            pinInProgressBState();
            const afterSecondB = snapshot();
            await settle(failureA, {status: 503, error: 'stale A failure'});
            unchanged(snapshot(), afterSecondB, 'stale A failure changed current B editor state');

            // A→B→A: neither the intervening B nor the earlier A owns the final A state.
            const firstA = start(100);
            const betweenB = start(200);
            const newestA = start(100);
            await settle(newestA, {bySubject: {'100': schema(9, 'Newest A schema')}});
            const afterNewestA = snapshot();
            await settle(betweenB, {bySubject: {'200': schema(9, 'Stale B schema')}});
            unchanged(snapshot(), afterNewestA, 'stale B success changed newest A editor state');
            await settle(firstA, {status: 503, error: 'stale first A failure'});
            unchanged(snapshot(), afterNewestA, 'earlier A failure changed newest A editor state');

            // Switching operations invalidates pending schema work and clears its spinner.
            const oldOperationLoad = start(100);
            state.operation = 'update_brand';
            state.clearFields();
            const afterOperationChange = snapshot();
            await settle(oldOperationLoad, {bySubject: {'100': schema(9, 'Late schema')}});
            unchanged(snapshot(), afterOperationChange, 'stale schema response changed operation state');

            // Clearing the subject starts an unfiltered request; the old A response stays stale.
            state.operation = 'update_characteristic';
            const oldSubjectLoad = start(100);
            state.clearCategoryFilter();
            const clearIndex = loads.length - 1;
            const clearSubjectLoad = {request: requests[clearIndex], promise: loads[clearIndex]};
            await settle(clearSubjectLoad, {common: schema(7, 'Common field')});
            const afterSubjectClear = snapshot();
            await settle(oldSubjectLoad, {
                bySubject: {'100': schema(9, 'Late A schema')},
                common: schema(8, 'Stale common field'),
            });
            unchanged(snapshot(), afterSubjectClear, 'stale subject response changed cleared filter state');

            return {
                passed: true,
                scenarios: [
                    'stale_success_after_A_to_B',
                    'stale_failure_after_A_to_B',
                    'stale_success_and_failure_after_A_to_B_to_A',
                    'operation_change_invalidates_pending_load',
                    'subject_clear_invalidates_prior_subject',
                ],
            };
        } finally {
            window.fetch = originalFetch;
        }
    }""")


def wait_for_layout_settle(page, *, width: int, theme: str) -> dict:
    result = page.evaluate("""async ({expectedWidth, theme, timeoutMs, stableFrames}) => {
        const started = performance.now();
        const boundedTimeout = Math.min(5000, Math.max(1, Number(timeoutMs) || 5000));
        const deadline = started + boundedTimeout;
        const tolerance = 0.75;
        const rounded = value => Math.round(value * 100) / 100;
        const fonts = document.fonts || null;
        let fontsSettled = !fonts;
        let fontsRejected = false;
        if (fonts) {
            Promise.resolve(fonts.ready).then(
                () => { fontsSettled = true; },
                () => { fontsSettled = true; fontsRejected = true; },
            );
        }
        const mainContent = document.querySelector('.main-content');
        const main = document.querySelector('#main-content');
        const sidebar = document.querySelector('.sidebar');
        if (!mainContent || !main || !sidebar) {
            throw new Error('responsive layout anchors are missing');
        }
        const relevantTransitionRunning = (element, properties) => {
            if (typeof element.getAnimations !== 'function') return false;
            return element.getAnimations({subtree: false}).some(animation =>
                animation.playState === 'running'
                && properties.includes(animation.transitionProperty)
            );
        };
        const measure = () => {
            const contentRect = mainContent.getBoundingClientRect();
            const mainRect = main.getBoundingClientRect();
            const sidebarRect = sidebar.getBoundingClientRect();
            return {
                viewportWidth: window.innerWidth,
                contentLeft: rounded(contentRect.left),
                contentWidth: rounded(contentRect.width),
                contentMarginLeft: rounded(parseFloat(getComputedStyle(mainContent).marginLeft) || 0),
                mainLeft: rounded(mainRect.left),
                mainWidth: rounded(mainRect.width),
                activeTheme: document.documentElement.getAttribute('data-theme') || '',
                sidebarLeft: rounded(sidebarRect.left),
                sidebarRight: rounded(sidebarRect.right),
                sidebarWidth: rounded(sidebarRect.width),
                documentWidth: document.documentElement.scrollWidth,
                bodyWidth: document.body.scrollWidth,
                fontStatus: fonts ? fonts.status : 'unsupported',
                fontsSettled,
                fontsRejected,
                mainMarginTransition: relevantTransitionRunning(mainContent, ['margin-left']),
                sidebarTransition: relevantTransitionRunning(sidebar, ['width', 'transform']),
            };
        };
        const stableAcrossFrames = (before, after) => {
            if (!before) return false;
            const numeric = [
                'viewportWidth', 'contentLeft', 'contentWidth', 'contentMarginLeft',
                'mainLeft', 'mainWidth', 'sidebarLeft', 'sidebarRight', 'sidebarWidth',
                'documentWidth', 'bodyWidth',
            ];
            return numeric.every(key => Math.abs(before[key] - after[key]) <= tolerance)
                && before.fontStatus === after.fontStatus
                && before.fontsSettled === after.fontsSettled
                && before.activeTheme === after.activeTheme;
        };
        const targetGeometryMatches = current => {
            if (current.viewportWidth !== expectedWidth) return false;
            if (expectedWidth < 1024) {
                return Math.abs(current.contentLeft) <= tolerance
                    && Math.abs(current.contentWidth - expectedWidth) <= tolerance
                    && Math.abs(current.contentMarginLeft) <= tolerance;
            }
            const availableWidth = expectedWidth - current.sidebarRight;
            return Math.abs(current.sidebarLeft) <= tolerance
                && Math.abs(current.contentLeft - current.sidebarRight) <= tolerance
                && Math.abs(current.contentMarginLeft - current.sidebarRight) <= tolerance
                && Math.abs(current.contentWidth - availableWidth) <= tolerance;
        };
        let previous = null;
        let stableCount = 0;
        let latest = null;
        while (performance.now() < deadline) {
            const remaining = Math.max(1, deadline - performance.now());
            let timer = null;
            const frameArrived = await Promise.race([
                new Promise(resolve => requestAnimationFrame(() => resolve(true))),
                new Promise(resolve => { timer = setTimeout(() => resolve(false), remaining); }),
            ]);
            if (timer !== null) clearTimeout(timer);
            if (!frameArrived) break;
            latest = measure();
            const transitionsRunning = latest.mainMarginTransition || latest.sidebarTransition;
            const fontsReady = latest.fontsSettled && latest.fontStatus === 'loaded';
            const themeReady = latest.activeTheme === theme;
            const geometryMatches = targetGeometryMatches(latest);
            if (
                !transitionsRunning && fontsReady && themeReady && geometryMatches
                && stableAcrossFrames(previous, latest)
            ) {
                stableCount += 1;
                if (stableCount >= stableFrames) {
                    return {
                        theme,
                        elapsed_ms: rounded(performance.now() - started),
                        stable_frames: stableCount,
                        fonts_ready: fontsReady,
                        theme_ready: themeReady,
                        transitions_running: false,
                        viewport_width: latest.viewportWidth,
                        main_content_left: latest.contentLeft,
                        main_content_width: latest.contentWidth,
                        sidebar_right: latest.sidebarRight,
                    };
                }
            } else {
                stableCount = 0;
            }
            previous = latest;
        }
        throw new Error(JSON.stringify({
            reason: 'layout_not_stable_before_deadline',
            expected_width: expectedWidth,
            theme,
            elapsed_ms: rounded(performance.now() - started),
            stable_frames: stableCount,
            latest,
        }));
    }""", arg={
        "expectedWidth": width,
        "theme": theme,
        "timeoutMs": 5000,
        "stableFrames": 3,
    })
    return result


def measure_single_edit_footer_state(page, *, state: str) -> dict:
    if state not in {"open", "closed"}:
        raise ValueError("footer clearance state must be open or closed")
    return page.evaluate("""state => {
        const rounded = value => Math.round(value * 100) / 100;
        const rect = element => {
            if (!element) return null;
            const box = element.getBoundingClientRect();
            return {
                x: rounded(box.left), y: rounded(box.top),
                width: rounded(box.width), height: rounded(box.height),
            };
        };
        const area = (left, right) => {
            if (!left || !right) return 0;
            const width = Math.max(0, Math.min(left.x + left.width, right.x + right.width)
                - Math.max(left.x, right.x));
            const height = Math.max(0, Math.min(left.y + left.height, right.y + right.height)
                - Math.max(left.y, right.y));
            return rounded(width * height);
        };
        const textRects = (element, text) => {
            if (!element) return [];
            const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                const offset = node.textContent.indexOf(text);
                if (offset < 0) continue;
                const range = document.createRange();
                range.setStart(node, offset);
                range.setEnd(node, offset + text.length);
                return Array.from(range.getClientRects()).map(box => ({
                    x: rounded(box.left), y: rounded(box.top),
                    width: rounded(box.width), height: rounded(box.height),
                }));
            }
            return [];
        };
        const bounds = fragments => {
            if (!fragments.length) return null;
            const left = Math.min(...fragments.map(item => item.x));
            const top = Math.min(...fragments.map(item => item.y));
            const right = Math.max(...fragments.map(item => item.x + item.width));
            const bottom = Math.max(...fragments.map(item => item.y + item.height));
            return {
                x: rounded(left), y: rounded(top),
                width: rounded(right - left), height: rounded(bottom - top),
            };
        };
        const contained = (inner, outer) => !!inner && !!outer
            && inner.x >= outer.x - 0.5
            && inner.y >= outer.y - 0.5
            && inner.x + inner.width <= outer.x + outer.width + 0.5
            && inner.y + inner.height <= outer.y + outer.height + 0.5;
        const form = document.querySelector('form.space-y-6');
        const footer = form?.querySelector('.sticky.bottom-0');
        const controls = [
            {name: 'save', element: footer?.querySelector('button[type="submit"]'), label: 'Сохранить в WB'},
            {name: 'cancel', element: footer?.querySelector('a'), label: 'Отмена'},
        ];
        const assistant = document.querySelector('.sh-ai-pop[data-route="product_edit"]');
        const trigger = assistant?.querySelector('.sh-ai-pop-trigger');
        const panel = assistant?.querySelector('.sh-ai-pop-panel');
        const panelVisible = !!panel && panel.getClientRects().length > 0
            && getComputedStyle(panel).display !== 'none'
            && getComputedStyle(panel).visibility !== 'hidden';
        const triggerRect = rect(trigger);
        const panelRect = panelVisible ? rect(panel) : null;
        const messageScroller = panel?.querySelector('.sh-ai-pop-messages');
        const composer = panel?.querySelector('.sh-ai-pop-composer textarea');
        const closeControl = panel?.querySelector('.sh-ai-pop-icon[aria-label]');
        const messageRect = rect(messageScroller);
        const composerRect = rect(composer);
        const closeControlRect = rect(closeControl);
        const panelContainsMessageScroller = contained(messageRect, panelRect);
        const panelContainsComposer = contained(composerRect, panelRect);
        const panelContainsCloseControl = contained(closeControlRect, panelRect);
        const messageStyle = messageScroller ? getComputedStyle(messageScroller) : null;
        const hitTest = (element, point, name) => {
            const hit = document.elementFromPoint(point.x, point.y);
            const assistantHit = !!hit && !!hit.closest('.sh-ai-pop-trigger, .sh-ai-pop-panel');
            return {
                state,
                point: {x: rounded(point.x), y: rounded(point.y)},
                topmost_is_control: !!hit && !!element && (hit === element || element.contains(hit)),
                topmost_is_assistant: assistantHit,
            };
        };
        const controlRows = controls.map(({name, element, label}) => {
            const buttonRect = rect(element);
            const labelRects = textRects(element, label);
            const labelRect = bounds(labelRects);
            const style = element ? getComputedStyle(element) : null;
            const rawText = element?.innerText || '';
            const points = [];
            const addPoint = (x, y) => {
                const point = {x: rounded(x), y: rounded(y)};
                if (!buttonRect || x < buttonRect.x || x >= buttonRect.x + buttonRect.width
                    || y < buttonRect.y || y >= buttonRect.y + buttonRect.height
                    || x < 0 || x >= innerWidth || y < 0 || y >= innerHeight) return;
                if (!points.some(item => Math.abs(item.x - point.x) < 0.5 && Math.abs(item.y - point.y) < 0.5)) {
                    points.push(point);
                }
            };
            for (const fragment of labelRects) {
                addPoint(fragment.x + fragment.width / 2, fragment.y + fragment.height / 2);
            }
            if (buttonRect) {
                addPoint(buttonRect.x + buttonRect.width / 2, buttonRect.y + buttonRect.height / 2);
            }
            const hitTests = points.map(point => hitTest(element, point, name));
            const triggerIntersectionArea = area(buttonRect, triggerRect);
            const panelIntersectionArea = panelVisible ? area(buttonRect, panelRect) : 0;
            return {
                name,
                visible: !!element && element.getClientRects().length > 0,
                enabled: !!element && !element.disabled,
                button_rect: buttonRect,
                label_rect: labelRect,
                label_rects: labelRects,
                label_text: rawText.replace(/\\s+/g, ' ').trim(),
                label_fully_inside_button: labelRects.length > 0
                    && labelRects.every(fragment => contained(fragment, buttonRect)),
                trigger_intersection_area_px: rounded(triggerIntersectionArea),
                panel_intersection_area_px: rounded(panelIntersectionArea),
                hit_tests: hitTests,
                focus_probe_target: name,
                outline_width_px: style ? Number.parseFloat(style.outlineWidth) || 0 : 0,
            };
        });
        const heading = document.querySelector('.max-w-7xl h1');
        const headingContainer = heading?.parentElement;
        const headingRect = rect(heading);
        const headingContainerRect = rect(headingContainer);
        const headingFragments = textRects(heading, 'Редактирование карточки');
        const root = document.documentElement;
        return {
            viewport: {width: innerWidth, height: innerHeight},
            root: {
                client_width: root.clientWidth,
                scroll_width: root.scrollWidth,
                overflow_px: Math.max(0, root.scrollWidth - innerWidth),
            },
            body: {
                client_width: document.body.clientWidth,
                scroll_width: document.body.scrollWidth,
                overflow_px: Math.max(0, document.body.scrollWidth - innerWidth),
            },
            heading: {
                rect: headingRect,
                container_rect: headingContainerRect,
                line_rects: headingFragments,
                window_scroll_y: rounded(window.scrollY),
                inside_viewport: headingFragments.length > 0
                    && headingFragments.every(fragment => fragment.x >= 0 && fragment.y >= 0
                        && fragment.x + fragment.width <= innerWidth + 0.5
                        && fragment.y + fragment.height <= innerHeight + 0.5),
                fully_inside_container: headingFragments.length > 0
                    && headingFragments.every(fragment => contained(fragment, headingContainerRect)),
            },
            actual_theme: document.documentElement.getAttribute('data-theme') || '',
            assistant: {
                trigger_rect: triggerRect,
                panel_rect: panelRect,
                panel_visible: panelVisible,
                trigger_expanded: trigger?.getAttribute('aria-expanded') === 'true',
                panel_inside_viewport: !!panelRect
                    && panelRect.x >= 0 && panelRect.y >= 0
                    && panelRect.x + panelRect.width <= innerWidth + 0.5
                    && panelRect.y + panelRect.height <= innerHeight + 0.5,
                message_scroll_region: {
                    rect: messageRect,
                    overflow_y: messageStyle?.overflowY || '',
                    client_height: messageScroller?.clientHeight || 0,
                    scroll_height: messageScroller?.scrollHeight || 0,
                    inside_panel: panelContainsMessageScroller,
                },
                composer_rect: composerRect,
                close_control_rect: closeControlRect,
                composer_inside_panel: panelContainsComposer,
                close_control_inside_panel: panelContainsCloseControl,
                trigger_intersection_area_px: Object.fromEntries(controlRows.map(row => [row.name, row.trigger_intersection_area_px])),
                panel_intersection_area_px: Object.fromEntries(controlRows.map(row => [row.name, row.panel_intersection_area_px])),
            },
            controls: controlRows,
        };
    }""", state)


def capture_single_edit_footer_clearance(
    page,
    *,
    single_edit_post_requests: list[dict[str, str]],
    browser_mutation_requests: list[dict[str, str]],
    product_edit_path: str,
) -> dict:
    """Measure the real WB sticky controls and shared assistant in six mobile states."""
    page.evaluate("""() => {
        window.__singleEditFooterSubmitEvents = 0;
        const form = document.querySelector('form.space-y-6');
        if (!form) throw new Error('single_edit_form_missing_for_footer_probe');
        form.addEventListener('submit', () => { window.__singleEditFooterSubmitEvents += 1; }, true);
    }""")

    def form_signature() -> str:
        return page.evaluate("""() => {
            const form = document.querySelector('form.space-y-6');
            const root = document.querySelector('[x-data^="productEditApp"]');
            if (!form || !root || !window.Alpine) throw new Error('single_edit_form_state_unavailable');
            return JSON.stringify({
                entries: Array.from(new FormData(form).entries()),
                has_changes: !!Alpine.$data(root).hasChanges,
            });
        }""")

    def focus_observation(selector: str) -> dict:
        return page.evaluate("""selector => {
            const element = document.querySelector(selector);
            if (!element) return {reached: false, visible: false, inside_viewport: false,
                outline_width_px: 0, outline_offset_px: 0, outline_style: ''};
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return {
                reached: document.activeElement === element,
                visible: element.matches(':focus-visible'),
                inside_viewport: box.left >= 0 && box.top >= 0
                    && box.right <= innerWidth && box.bottom <= innerHeight,
                outline_width_px: Number.parseFloat(style.outlineWidth) || 0,
                outline_offset_px: Number.parseFloat(style.outlineOffset) || 0,
                outline_style: style.outlineStyle,
            };
        }""", selector)

    def rounded_rect(rect: dict | None) -> dict | None:
        if not rect:
            return None
        return {
            key: round(float(rect[key]), 2)
            for key in ("x", "y", "width", "height")
        }

    def no_intersections(values: dict | None) -> bool:
        return isinstance(values, dict) and set(values) == {"save", "cancel"} and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0
            for value in values.values()
        )

    REPORT["single_edit_footer_clearance"].clear()
    for width in (320, 360, 390):
        for theme in ("light", "dark"):
            page.set_viewport_size({"width": width, "height": 844})
            page.evaluate(
                "theme => document.documentElement.setAttribute('data-theme', theme)",
                theme,
            )
            page.evaluate("""() => {
                if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
                document.body.scrollTop = 0;
            }""")
            wait_for_layout_settle(page, width=width, theme=theme)
            page.evaluate("""() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
            heading_state = measure_single_edit_footer_state(page, state="closed")

            page.evaluate("""() => {
                const scrolling = document.scrollingElement;
                if (scrolling) scrolling.scrollTop = scrolling.scrollHeight;
                document.body.scrollTop = document.body.scrollHeight;
            }""")
            page.evaluate("""() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
            page.mouse.move(2, 2)

            before_form = form_signature()
            before_mutations = len(browser_mutation_requests)
            before_posts = len(single_edit_post_requests)
            before_provider_attempts = REPORT["provider_attempts"]
            before_submit_events = page.evaluate("window.__singleEditFooterSubmitEvents")
            closed_before = measure_single_edit_footer_state(page, state="closed")
            if closed_before["assistant"]["panel_visible"]:
                raise AssertionError("assistant_panel_was_open_before_footer_probe")

            trigger = page.locator('.sh-ai-pop[data-route="product_edit"] .sh-ai-pop-trigger')
            panel = page.locator('.sh-ai-pop[data-route="product_edit"] .sh-ai-pop-panel')
            trigger.click()
            panel.wait_for(state="visible")
            page.wait_for_timeout(240)
            page.mouse.move(2, 2)
            opened = measure_single_edit_footer_state(page, state="open")

            trigger.click()
            panel.wait_for(state="hidden")
            page.wait_for_timeout(170)
            page.mouse.move(2, 2)
            closed_after = measure_single_edit_footer_state(page, state="closed")

            cancel_selector = "form.space-y-6 .wb-single-edit-actions a"
            save_selector = 'form.space-y-6 .wb-single-edit-actions button[type="submit"]'
            page.locator(cancel_selector).focus()
            page.keyboard.press("Tab")
            save_focus = focus_observation(save_selector)
            page.keyboard.press("Shift+Tab")
            cancel_focus = focus_observation(cancel_selector)
            page.mouse.move(2, 2)

            after_form = form_signature()
            mutation_slice = browser_mutation_requests[before_mutations:]
            generation_requests = sum(
                request["method"] == "POST"
                and request["path"].startswith("/agents/api/conversations")
                for request in mutation_slice
            )
            form_post_count = sum(
                request["method"] == "POST"
                and request["path"] == product_edit_path
                for request in single_edit_post_requests[before_posts:]
            )
            controls_by_state = {
                state: {row["name"]: row for row in snapshot["controls"]}
                for state, snapshot in (("closed", closed_after), ("open", opened))
            }
            control_rows = []
            for name, label in (("save", "Сохранить в WB"), ("cancel", "Отмена")):
                closed_control = controls_by_state["closed"].get(name, {})
                open_control = controls_by_state["open"].get(name, {})
                keyboard = save_focus if name == "save" else cancel_focus
                control_rows.append({
                    "name": name,
                    "visible": closed_control.get("visible") is True,
                    "enabled": closed_control.get("enabled") is True,
                    "button_rect": rounded_rect(closed_control.get("button_rect")),
                    "label_rect": rounded_rect(closed_control.get("label_rect")),
                    "label_rects": [
                        rounded_rect(rect) for rect in closed_control.get("label_rects", [])
                    ],
                    "label_text": closed_control.get("label_text", ""),
                    "label_fully_inside_button": closed_control.get("label_fully_inside_button") is True,
                    "assistant_open_intersection_area_px": open_control.get("trigger_intersection_area_px"),
                    "assistant_closed_intersection_area_px": closed_control.get("trigger_intersection_area_px"),
                    "open_panel_intersection_area_px": open_control.get("panel_intersection_area_px"),
                    "hit_tests": (
                        closed_control.get("hit_tests", [])
                        + open_control.get("hit_tests", [])
                    ),
                    "keyboard_focus": keyboard,
                })

            assistant_open = opened["assistant"]
            assistant_closed = closed_after["assistant"]
            viewport = opened["viewport"]
            row = {
                "requested_theme": theme,
                "actual_theme": opened["actual_theme"],
                "viewport": viewport,
                "root": opened["root"],
                "body": opened["body"],
                "heading": heading_state["heading"],
                "assistant": {
                    "opened_via_trigger": (
                        assistant_open["panel_visible"] is True
                        and assistant_open["trigger_expanded"] is True
                    ),
                    "closed_via_trigger": (
                        assistant_closed["panel_visible"] is False
                        and assistant_closed["trigger_expanded"] is False
                    ),
                    "open_rect": rounded_rect(assistant_open.get("trigger_rect")),
                    "closed_trigger_rect": rounded_rect(assistant_closed.get("trigger_rect")),
                    "open_panel_rect": rounded_rect(assistant_open.get("panel_rect")),
                    "closed_panel_hidden": assistant_closed["panel_visible"] is False,
                    "open_panel_inside_viewport": assistant_open["panel_inside_viewport"] is True,
                    "open_intersection_area_px": assistant_open["trigger_intersection_area_px"],
                    "closed_intersection_area_px": assistant_closed["trigger_intersection_area_px"],
                    "open_panel_intersection_area_px": assistant_open["panel_intersection_area_px"],
                    "message_scroll_region": assistant_open["message_scroll_region"],
                    "composer_rect": rounded_rect(assistant_open.get("composer_rect")),
                    "close_control_rect": rounded_rect(assistant_open.get("close_control_rect")),
                    "composer_inside_panel": assistant_open["composer_inside_panel"] is True,
                    "close_control_inside_panel": assistant_open["close_control_inside_panel"] is True,
                },
                "controls": control_rows,
                "side_effects": {
                    "generation_requests": generation_requests,
                    "form_post_count": form_post_count,
                    "provider_attempts": REPORT["provider_attempts"] - before_provider_attempts,
                    "browser_mutations": len(mutation_slice),
                    "form_unchanged": before_form == after_form,
                    "form_submit_events": (
                        page.evaluate("window.__singleEditFooterSubmitEvents") - before_submit_events
                    ),
                },
            }
            REPORT["single_edit_footer_clearance"].append(row)

    expected_pairs = [
        (width, theme)
        for width in (320, 360, 390)
        for theme in ("light", "dark")
    ]
    actual_pairs = [
        (row.get("viewport", {}).get("width"), row.get("actual_theme"))
        for row in REPORT["single_edit_footer_clearance"]
    ]
    failures = []
    for row in REPORT["single_edit_footer_clearance"]:
        width = row["viewport"]["width"]
        theme = row["requested_theme"]
        prefix = {"width": width, "theme": theme}
        if row["actual_theme"] != theme:
            failures.append({**prefix, "reason": "theme_mismatch"})
        if row["viewport"]["height"] != 844:
            failures.append({**prefix, "reason": "viewport_height_mismatch"})
        if any(
            row[part]["overflow_px"] != 0
            or row[part]["scroll_width"] > width
            for part in ("root", "body")
        ):
            failures.append({**prefix, "reason": "horizontal_overflow"})
        heading = row["heading"]
        if (
            heading["window_scroll_y"] != 0
            or heading["inside_viewport"] is not True
            or heading["fully_inside_container"] is not True
        ):
            failures.append({**prefix, "reason": "heading_not_wrapped_within_viewport"})
        assistant = row["assistant"]
        if (
            assistant["opened_via_trigger"] is not True
            or assistant["closed_via_trigger"] is not True
            or assistant["closed_panel_hidden"] is not True
            or assistant["open_panel_inside_viewport"] is not True
            or assistant["composer_inside_panel"] is not True
            or assistant["close_control_inside_panel"] is not True
        ):
            failures.append({**prefix, "reason": "assistant_panel_not_accessible"})
        message_region = assistant["message_scroll_region"]
        if (
            message_region["inside_panel"] is not True
            or message_region["overflow_y"] not in {"auto", "scroll"}
            or message_region["client_height"] <= 0
            or message_region["scroll_height"] < message_region["client_height"]
        ):
            failures.append({**prefix, "reason": "assistant_message_region_not_locally_scrollable"})
        if not no_intersections(assistant["open_intersection_area_px"]):
            failures.append({**prefix, "reason": "open_assistant_trigger_overlaps_footer"})
        if not no_intersections(assistant["closed_intersection_area_px"]):
            failures.append({**prefix, "reason": "closed_assistant_trigger_overlaps_footer"})
        if not no_intersections(assistant["open_panel_intersection_area_px"]):
            failures.append({**prefix, "reason": "assistant_panel_overlaps_footer"})
        if [control["name"] for control in row["controls"]] != ["save", "cancel"]:
            failures.append({**prefix, "reason": "footer_control_set_mismatch"})
        for control in row["controls"]:
            button = control.get("button_rect") or {}
            labels = control.get("label_rects") or []
            points = control.get("hit_tests") or []
            focus = control.get("keyboard_focus") or {}
            if (
                control.get("visible") is not True
                or control.get("enabled") is not True
                or button.get("width", 0) < 44
                or button.get("height", 0) < 44
            ):
                failures.append({**prefix, "control": control["name"], "reason": "target_below_44px"})
            if (
                control.get("label_text") != (
                    "Сохранить в WB" if control["name"] == "save" else "Отмена"
                )
                or control.get("label_fully_inside_button") is not True
                or not labels
            ):
                failures.append({**prefix, "control": control["name"], "reason": "label_not_fully_visible"})
            if (
                control.get("assistant_open_intersection_area_px") != 0
                or control.get("assistant_closed_intersection_area_px") != 0
                or control.get("open_panel_intersection_area_px") != 0
            ):
                failures.append({**prefix, "control": control["name"], "reason": "assistant_intersects_control"})
            expected_hit_count = 2 * (1 + len(labels))
            if (
                len(points) != expected_hit_count
                or any(
                    point.get("topmost_is_control") is not True
                    or point.get("topmost_is_assistant") is not False
                    for point in points
                )
            ):
                failures.append({**prefix, "control": control["name"], "reason": "hit_test_mismatch"})
            if (
                focus.get("reached") is not True
                or focus.get("visible") is not True
                or focus.get("inside_viewport") is not True
                or focus.get("outline_width_px", 0) < 2
                or focus.get("outline_offset_px", 0) < 2
                or focus.get("outline_style") != "solid"
            ):
                failures.append({**prefix, "control": control["name"], "reason": "keyboard_focus_not_visible"})
        effects = row["side_effects"]
        if (
            effects["generation_requests"] != 0
            or effects["form_post_count"] != 0
            or effects["form_submit_events"] != 0
            or effects["provider_attempts"] != 0
            or effects["browser_mutations"] != 0
            or effects["form_unchanged"] is not True
        ):
            failures.append({**prefix, "reason": "side_effect_during_clearance_probe"})

    all_rows_measured = (
        len(REPORT["single_edit_footer_clearance"]) == 6
        and actual_pairs == expected_pairs
        and len(set(actual_pairs)) == 6
    )
    if not all_rows_measured:
        failures.append({"reason": "six_row_matrix_mismatch"})
    all_controls_clear = not any(
        failure.get("reason") in {
            "horizontal_overflow", "assistant_panel_not_accessible",
            "assistant_message_region_not_locally_scrollable",
            "open_assistant_trigger_overlaps_footer", "closed_assistant_trigger_overlaps_footer",
            "assistant_panel_overlaps_footer", "footer_control_set_mismatch",
            "target_below_44px", "label_not_fully_visible", "assistant_intersects_control",
            "hit_test_mismatch", "heading_not_wrapped_within_viewport",
        }
        for failure in failures
    )
    all_keyboard_focus_visible = not any(
        failure.get("reason") == "keyboard_focus_not_visible" for failure in failures
    )
    no_side_effects = not any(
        failure.get("reason") == "side_effect_during_clearance_probe" for failure in failures
    )
    passed = all_rows_measured and all_controls_clear and all_keyboard_focus_visible and no_side_effects
    page.set_viewport_size({"width": 1280, "height": 900})
    page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", "light")
    page.evaluate("() => { if (document.scrollingElement) document.scrollingElement.scrollTop = 0; }")
    return {
        "name": "single_edit_footer_assistant_clearance_320_360_390_light_dark",
        "status": "passed" if passed else "failed",
        "viewports": [320, 360, 390],
        "themes": ["light", "dark"],
        "row_count": len(REPORT["single_edit_footer_clearance"]),
        "all_rows_measured": all_rows_measured,
        "all_controls_clear": all_controls_clear,
        "all_keyboard_focus_visible": all_keyboard_focus_visible,
        "no_side_effects": no_side_effects,
        "failures": failures,
    }


def capture_wb_editor_control_contrast(page, *, theme: str) -> dict:
    """Capture computed color/opacity contrast for enabled WB edit controls.

    This is diagnostic telemetry only; it does not add a layout or interaction
    receipt and does not change the existing browser gate matrix.
    """
    return page.evaluate("""expectedTheme => {
        const selectors = {
            cancel: '.sticky.bottom-0 a[href^="/products/"]',
            optional_picker_label: 'label[for="wb-optional-characteristic-picker"]',
            optional_picker: '#wb-optional-characteristic-picker',
            optional_add: '#wb-add-optional-characteristic',
            save: 'form.space-y-6 button[type="submit"]',
        };
        const parseColor = value => {
            const match = String(value || '').match(
                /^rgba?\\(\\s*([\\d.]+)[, ]+([\\d.]+)[, ]+([\\d.]+)(?:\\s*[,/]+\\s*([\\d.]+%?))?\\s*\\)$/i
            );
            if (!match) return null;
            const alpha = match[4] == null ? 1 : (
                match[4].endsWith('%') ? Number(match[4].slice(0, -1)) / 100 : Number(match[4])
            );
            return [Number(match[1]), Number(match[2]), Number(match[3]), alpha];
        };
        const over = (front, back) => {
            const alpha = Math.max(0, Math.min(1, front[3]));
            return [0, 1, 2].map(index => front[index] * alpha + back[index] * (1 - alpha));
        };
        const mix = (front, back, alpha) => [0, 1, 2].map(index =>
            front[index] * alpha + back[index] * (1 - alpha)
        );
        const luminance = rgb => {
            const channel = value => {
                const normalized = value / 255;
                return normalized <= 0.04045
                    ? normalized / 12.92
                    : Math.pow((normalized + 0.055) / 1.055, 2.4);
            };
            const [r, g, b] = rgb.map(channel);
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        };
        const ratio = (first, second) => {
            const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
            return Math.round(((values[0] + 0.05) / (values[1] + 0.05)) * 1000) / 1000;
        };
        const roundedRgb = value => value.map(channel => Math.round(channel * 1000) / 1000);
        const canvas = [255, 255, 255];
        const records = Object.entries(selectors).map(([name, selector]) => {
            const element = document.querySelector(selector);
            if (!element) return {name, selector, missing: true};
            const ancestors = [];
            for (let node = element; node; node = node.parentElement) ancestors.push(node);
            ancestors.reverse();
            let parentBackground = [...canvas];
            const layers = ancestors.map(node => {
                const style = getComputedStyle(node);
                const parsed = parseColor(style.backgroundColor);
                const before = [...parentBackground];
                if (parsed && parsed[3] > 0) parentBackground = over(parsed, parentBackground);
                return {
                    tag: node.tagName,
                    id: node.id || '',
                    background_color: style.backgroundColor,
                    background_rgb_after_compositing: roundedRgb(parentBackground),
                    background_image: style.backgroundImage,
                    opacity: Number(style.opacity),
                    filter: style.filter,
                    backdrop_filter: style.backdropFilter || style.webkitBackdropFilter || 'none',
                    mix_blend_mode: style.mixBlendMode,
                    has_background_image: style.backgroundImage !== 'none',
                    background_changed: before.some((value, index) => value !== parentBackground[index]),
                };
            });
            const style = getComputedStyle(element);
            const effectiveBackgroundBeforeOpacity = parentBackground;
            const ownForeground = parseColor(style.color);
            const localForeground = ownForeground
                ? over(ownForeground, effectiveBackgroundBeforeOpacity)
                : null;
            const opacityChain = ancestors.map(node => ({
                tag: node.tagName,
                id: node.id || '',
                opacity: Number(getComputedStyle(node).opacity),
            }));
            const opacityProduct = opacityChain.reduce((product, item) => product * item.opacity, 1);
            const effectiveBackground = mix(effectiveBackgroundBeforeOpacity, canvas, opacityProduct);
            const effectiveForeground = localForeground
                ? mix(localForeground, canvas, opacityProduct)
                : null;
            const rect = element.getBoundingClientRect();
            const enabled = !('disabled' in element) || !element.disabled;
            return {
                name,
                selector,
                text: (element.innerText || element.getAttribute('aria-label') || '').trim(),
                visible: !!element.getClientRects().length
                    && style.display !== 'none'
                    && style.visibility !== 'hidden',
                in_viewport: rect.bottom > 0 && rect.right > 0
                    && rect.top < innerHeight && rect.left < innerWidth,
                enabled,
                disabled: 'disabled' in element ? !!element.disabled : false,
                computed_color: style.color,
                computed_background_color: style.backgroundColor,
                computed_opacity: style.opacity,
                opacity_product: Math.round(opacityProduct * 10000) / 10000,
                opacity_chain: opacityChain,
                effective_background_rgb: roundedRgb(effectiveBackground),
                effective_foreground_rgb: effectiveForeground ? roundedRgb(effectiveForeground) : null,
                contrast_ratio_estimate: effectiveForeground
                    ? ratio(effectiveForeground, effectiveBackground)
                    : null,
                normal_text_wcag_aa: effectiveForeground
                    ? ratio(effectiveForeground, effectiveBackground) >= 4.5
                    : null,
                background_layers: layers,
                ancestor_effects: {
                    has_background_image: layers.some(layer => layer.has_background_image),
                    has_filter: layers.some(layer => layer.filter !== 'none'),
                    has_backdrop_filter: layers.some(layer => layer.backdrop_filter !== 'none'),
                    has_non_normal_blend: layers.some(layer => layer.mix_blend_mode !== 'normal'),
                },
            };
        });
        return {
            requested_theme: expectedTheme,
            actual_theme: document.documentElement.getAttribute('data-theme') || '',
            viewport: {width: innerWidth, height: innerHeight},
            measurement: 'computed CSS colors composited from ancestor background colors; opacity product applied against white canvas; background images/filters reported separately',
            controls: records,
        };
    }""", theme)


def wait_for_wb_editor_appearance_settle(page) -> dict:
    """Wait for actual color/background/opacity transitions to finish."""
    return page.evaluate("""async () => {
        const selectors = [
            '.sticky.bottom-0 a[href^="/products/"]',
            'label[for="wb-optional-characteristic-picker"]',
            '#wb-optional-characteristic-picker',
            '#wb-add-optional-characteristic',
            'form.space-y-6 button[type="submit"]',
        ];
        const relevant = new Set([
            'color', 'background', 'background-color', 'border-color', 'opacity',
            'box-shadow', 'outline-color', 'all',
        ]);
        const nodes = new Set();
        for (const selector of selectors) {
            for (let node = document.querySelector(selector); node; node = node.parentElement) {
                nodes.add(node);
            }
        }
        const activeTransitions = () => {
            const results = [];
            for (const node of nodes) {
                const animations = typeof node.getAnimations === 'function'
                    ? node.getAnimations({subtree: false}) : [];
                for (const animation of animations) {
                    const property = animation.transitionProperty || '';
                    if (animation.playState === 'running' && relevant.has(property)) {
                        results.push({
                            tag: node.tagName,
                            id: node.id || '',
                            transition_property: property,
                            play_state: animation.playState,
                        });
                    }
                }
            }
            return results;
        };
        const sample = () => selectors.map(selector => {
            const element = document.querySelector(selector);
            if (!element) return {selector, missing: true};
            const style = getComputedStyle(element);
            return {
                selector,
                color: style.color,
                background_color: style.backgroundColor,
                border_color: style.borderColor,
                opacity: style.opacity,
                box_shadow: style.boxShadow,
                outline_color: style.outlineColor,
            };
        });
        const started = performance.now();
        const deadline = started + 3500;
        let previous = null;
        let stableFrames = 0;
        let samples = 0;
        let finalSample = null;
        let active = [];
        while (performance.now() < deadline) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            finalSample = sample();
            active = activeTransitions();
            samples += 1;
            const same = previous !== null
                && JSON.stringify(previous) === JSON.stringify(finalSample);
            stableFrames = !active.length && same ? stableFrames + 1 : 0;
            previous = finalSample;
            if (!active.length && stableFrames >= 3) {
                return {
                    settled: true,
                    samples,
                    stable_frames: stableFrames,
                    elapsed_ms: Math.round((performance.now() - started) * 10) / 10,
                    active_relevant_transitions: [],
                    final_computed_styles: finalSample,
                };
            }
        }
        return {
            settled: false,
            samples,
            stable_frames: stableFrames,
            elapsed_ms: Math.round((performance.now() - started) * 10) / 10,
            active_relevant_transitions: active,
            final_computed_styles: finalSample,
        };
    }""")


def collect_overflow_diagnostics(
    page, *, label: str, theme: str, width: int, geometry: dict,
) -> dict:
    return page.evaluate("""({label, theme, width, geometry}) => {
        const viewport = window.innerWidth;
        const rounded = value => Math.round(value * 100) / 100;
        const styleText = value => String(value || '').slice(0, 160);
        const describe = element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return {
                tag: element.tagName.toLowerCase(),
                id: (element.id || '').slice(0, 80),
                classes: typeof element.className === 'string' ? element.className.slice(0, 180) : '',
                rect: {
                    left: rounded(rect.left), right: rounded(rect.right),
                    width: rounded(rect.width),
                },
                clientWidth: element.clientWidth,
                scrollWidth: element.scrollWidth,
                style: {
                    display: styleText(style.display),
                    position: styleText(style.position),
                    boxSizing: styleText(style.boxSizing),
                    width: styleText(style.width),
                    minWidth: styleText(style.minWidth),
                    maxWidth: styleText(style.maxWidth),
                    overflowX: styleText(style.overflowX),
                    whiteSpace: styleText(style.whiteSpace),
                    flex: styleText(style.flex),
                    flexBasis: styleText(style.flexBasis),
                    flexShrink: styleText(style.flexShrink),
                    gridTemplateColumns: styleText(style.gridTemplateColumns),
                    paddingLeft: styleText(style.paddingLeft),
                    paddingRight: styleText(style.paddingRight),
                },
            };
        };
        const all = Array.from(document.body.querySelectorAll('*'));
        const spillsRight = all.map(element => ({element, rect: element.getBoundingClientRect()}))
            .filter(item => item.rect.width > 0 && item.rect.right > viewport + 1)
            .sort((a, b) => b.rect.right - a.rect.right);
        const firstSpillingChildren = spillsRight.filter(({element}) => {
            const parent = element.parentElement;
            return !parent || parent.getBoundingClientRect().right <= viewport + 1;
        }).slice(0, 12).map(({element}) => {
            const ancestors = [];
            for (let parent = element.parentElement; parent && ancestors.length < 6; parent = parent.parentElement) {
                ancestors.push(describe(parent));
            }
            return {element: describe(element), ancestors};
        });
        const scrollContainers = all.filter(element => {
            const style = getComputedStyle(element);
            return ['auto', 'scroll'].includes(style.overflowX)
                && element.scrollWidth > element.clientWidth + 1;
        }).sort((a, b) => {
            return (b.scrollWidth - b.clientWidth) - (a.scrollWidth - a.clientWidth);
        }).slice(0, 8).map(describe);
        const main = document.querySelector('#main-content');
        return {
            page: label,
            theme,
            viewport_width: width,
            geometry,
            viewport_inner_width: viewport,
            document: {
                client_width: document.documentElement.clientWidth,
                scroll_width: document.documentElement.scrollWidth,
            },
            body: {
                client_width: document.body.clientWidth,
                scroll_width: document.body.scrollWidth,
            },
            main: main ? describe(main) : null,
            first_spilling_children: firstSpillingChildren,
            largest_right_edges: spillsRight.slice(0, 12).map(({element}) => describe(element)),
            horizontal_scroll_containers: scrollContainers,
        };
    }""", {
        "label": label,
        "theme": theme,
        "width": width,
        "geometry": geometry,
    })


def _assert_local_products_href(href: str, expected_query: dict[str, list[str]]):
    parsed = urlsplit(href)
    assert (
        not parsed.scheme and not parsed.netloc
        and parsed.path == "/products" and not parsed.fragment
    ), {"href": href[:512], "path": parsed.path}
    query = parse_qs(parsed.query, keep_blank_values=True)
    assert query == expected_query, {"href": href[:512], "query": query}
    return parsed


def _assert_local_products_url(url: str, expected_query: dict[str, list[str]]):
    parsed = urlsplit(url)
    origin = urlsplit(BASE or "")
    assert (
        origin.scheme == "http" and origin.netloc
        and parsed.scheme == origin.scheme and parsed.netloc == origin.netloc
        and parsed.path == "/products" and not parsed.fragment
    ), {"path": parsed.path, "scheme_matches": parsed.scheme == origin.scheme,
        "origin_matches": parsed.netloc == origin.netloc}
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in EMPTY_PRODUCT_FORM_FILTERS:
        if key not in query:
            continue
        if query[key] != [""]:
            raise AssertionError({"path": parsed.path, "unexpected_filter_value": key})
        del query[key]
    assert query == expected_query, {"path": parsed.path, "query": query}
    return parsed


def _bounded_failure_context(page) -> dict:
    parsed = urlsplit(page.url)
    raw_query = parse_qs(parsed.query, keep_blank_values=True)
    query = {
        str(key)[:80]: [str(value)[:120] for value in values[:4]]
        for key, values in list(raw_query.items())[:16]
    }
    try:
        title = str(page.title())[:160]
    except Exception:
        title = ""
    return {"path": parsed.path[:256], "query": query, "title": title}


def capture_layout(page, label: str) -> None:
    if label not in {
        "products_list", "bulk_editor", "bulk_review",
        "single_product_edit", "unmapped_product_edit",
    }:
        raise AssertionError(f"Unexpected WB browser page label: {label!r}")
    ARTIFACTS_PATH.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        page.evaluate(
            "theme => document.documentElement.setAttribute('data-theme', theme)",
            arg=theme,
        )
        wait_for_layout_settle(page, width=1280, theme=theme)
        for width in (390, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            settled = wait_for_layout_settle(page, width=width, theme=theme)
            geometry = page.evaluate("""() => ({
                viewport_width: window.innerWidth,
                document_width: document.documentElement.scrollWidth,
                body_width: document.body.scrollWidth,
                main_width: document.querySelector('main')?.getBoundingClientRect().width ?? null,
                main_content_left: document.querySelector('.main-content')?.getBoundingClientRect().left ?? null,
                main_content_width: document.querySelector('.main-content')?.getBoundingClientRect().width ?? null,
            })""")
            if (
                geometry["viewport_width"] != width
                or abs(geometry["main_content_left"] - settled["main_content_left"]) > 0.75
                or abs(geometry["main_content_width"] - settled["main_content_width"]) > 0.75
            ):
                raise AssertionError(
                    f"Responsive layout changed after settle on {label} ({theme}, {width}px): "
                    f"{geometry}; settle={settled}"
                )
            if geometry["document_width"] > width or geometry["body_width"] > width:
                try:
                    diagnostics = collect_overflow_diagnostics(
                        page, label=label, theme=theme, width=width, geometry=geometry,
                    )
                except Exception as exc:
                    diagnostics = {
                        "page": label,
                        "theme": theme,
                        "viewport_width": width,
                        "geometry": geometry,
                        "diagnostic_error": type(exc).__name__,
                    }
                REPORT["overflow_diagnostics"].append(diagnostics)
                raise AssertionError(
                    f"Horizontal overflow on {label} ({theme}, {width}px): {geometry}; "
                    f"bounded element diagnostics captured"
                )
            REPORT["layouts"].append({
                "page": label, "theme": theme, **geometry,
                "layout_settle": settled,
            })
            if width in (390, 1280):
                filename = f"wb-edit-{label.replace('_', '-')}-{theme}-{width}.png"
                screenshot = ARTIFACTS_PATH / filename
                page.screenshot(
                    path=str(screenshot), full_page=True,
                    timeout=10000,
                )
                REPORT["artifacts"].append({
                    "kind": "layout_screenshot",
                    "path": filename,
                    "page": label,
                    "theme": theme,
                    "width": width,
                })
        check(
            f"responsive_no_horizontal_overflow_{label}_{theme}",
            widths=[390, 768, 1280],
        )
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
    page.set_viewport_size({"width": 1280, "height": 900})


def assert_keyboard_focus(page, label: str) -> None:
    page.evaluate("document.activeElement?.blur()")
    page.keyboard.press("Tab")
    focus = page.evaluate("""() => {
        const element = document.activeElement;
        const rect = element?.getBoundingClientRect();
        return {
            tag: element?.tagName || null,
            id: element?.id || null,
            role: element?.getAttribute('role') || null,
            text: (element?.innerText || element?.getAttribute('aria-label') || '').trim().slice(0, 80),
            keyboard_visible: !!element && element.matches(':focus-visible'),
            rendered: !!rect && rect.width > 0 && rect.height > 0,
        };
    }""")
    assert focus["tag"] not in (None, "BODY") and focus["keyboard_visible"] and focus["rendered"], focus
    check(f"keyboard_focus_visible_{label}", **focus)


def assert_review_summary(page, *, changed: str, skipped: str = "0") -> None:
    cards = page.locator("section[aria-label='Сводка предпросмотра']").inner_text()
    assert "Выбрано" in cards and "50" in cards, cards
    assert "Изменится" in cards and changed in cards, cards
    assert "Пропущено" in cards and skipped in cards, cards
    assert "Ошибки" in cards and "0" in cards, cards
    assert "Предпросмотр" in page.content() and "не отправляет изменения в Wildberries" in page.content()


def assert_bulk_review_mobile_projection(
    page, fixture: dict[str, int], expected_sync: datetime,
) -> None:
    """Check the narrow review list, exact evidence, and keyboard scrolling."""
    page.set_viewport_size({"width": 320, "height": 800})
    settled = wait_for_layout_settle(page, width=320, theme="light")
    region = page.locator("[data-review-mobile-region]")
    desktop_table = page.locator("[data-review-desktop-table]")
    assert region.is_visible(), "The narrow review projection must be visible at 320px"
    assert not desktop_table.is_visible(), "The wide table must be hidden at 320px"
    assert_review_summary(page, changed="50")

    mobile_products = page.locator("[data-review-product-id-mobile]")
    mobile_changes = page.locator("[data-review-product-change-mobile]")
    mobile_exclusions = page.locator(
        "[data-review-mobile-projection] button[data-review-exclude-product-id]",
    )
    assert mobile_products.count() == 50, mobile_products.count()
    assert mobile_changes.count() == 50, mobile_changes.count()
    assert mobile_exclusions.count() == 50, mobile_exclusions.count()
    product_ids = mobile_products.evaluate_all(
        "nodes => nodes.map(node => node.getAttribute('data-review-product-id-mobile'))",
    )
    assert len(set(product_ids)) == 50, product_ids

    reviewed_identity = page.locator(
        f'[data-review-product-id-mobile="{fixture["product_id"]}"]',
    )
    assert reviewed_identity.count() == 1
    identity_text = reviewed_identity.inner_text()
    assert "Pipedream Synthetic Product 000" in identity_text
    assert "Артикул: PD-000" in identity_text
    assert f"ID Seller Hub: {fixture['product_id']}" in identity_text
    assert "nmID WB: 900000" in identity_text
    assert reviewed_identity.locator("img").get_attribute("src") == (
        "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
    )
    review_time = reviewed_identity.locator("time")
    assert review_time.count() == 1
    assert review_time.get_attribute("datetime") == expected_sync.isoformat() + "Z"
    assert review_time.inner_text() == expected_sync.strftime("%d.%m.%Y %H:%M UTC")

    first_change = reviewed_identity.locator("[data-review-product-change-mobile]")
    assert first_change.count() == 1
    change_text = first_change.inner_text()
    assert "Было" in change_text and "Pipedream" in change_text, change_text
    assert "Будет" in change_text and "Synthetic reviewed brand" in change_text, change_text

    geometry = page.evaluate("""() => {
        const region = document.querySelector('[data-review-mobile-region]');
        const desktop = document.querySelector('[data-review-desktop-table]');
        const bounded = [...document.querySelectorAll(
            '[data-review-product-id-mobile], [data-review-product-change-mobile]'
        )].map(element => ({
            scrollWidth: element.scrollWidth,
            clientWidth: element.clientWidth,
        }));
        const boxes = element => {
            const rect = element.getBoundingClientRect();
            return {width: rect.width, height: rect.height, left: rect.left, right: rect.right};
        };
        const back = document.querySelector('[data-review-mobile-back]');
        const apply = document.querySelector('[data-review-mobile-apply]');
        const exclude = document.querySelector(
            '[data-review-mobile-projection] button[data-review-exclude-product-id]'
        );
        return {
            viewportWidth: window.innerWidth,
            documentWidth: document.documentElement.scrollWidth,
            bodyWidth: document.body.scrollWidth,
            regionScrollWidth: region?.scrollWidth ?? null,
            regionClientWidth: region?.clientWidth ?? null,
            regionScrollHeight: region?.scrollHeight ?? null,
            regionClientHeight: region?.clientHeight ?? null,
            desktopDisplay: desktop ? getComputedStyle(desktop).display : null,
            bounded,
            back: back ? boxes(back) : null,
            apply: apply ? boxes(apply) : null,
            exclude: exclude ? boxes(exclude) : null,
        };
    }""")
    assert geometry["viewportWidth"] == 320, geometry
    assert geometry["documentWidth"] <= 320 and geometry["bodyWidth"] <= 320, geometry
    assert geometry["regionScrollWidth"] <= geometry["regionClientWidth"] + 1, geometry
    assert geometry["regionScrollHeight"] > geometry["regionClientHeight"], geometry
    assert geometry["desktopDisplay"] == "none", geometry
    assert all(row["scrollWidth"] <= row["clientWidth"] + 1 for row in geometry["bounded"]), geometry
    for name in ("back", "apply", "exclude"):
        box = geometry[name]
        assert box and box["width"] >= 44 and box["height"] >= 44, geometry
        assert box["left"] >= -0.75 and box["right"] <= 320.75, geometry

    region.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    focus = page.evaluate("""() => {
        const region = document.querySelector('[data-review-mobile-region]');
        const style = getComputedStyle(region);
        return {
            focused: document.activeElement === region,
            keyboardVisible: region.matches(':focus-visible'),
            outlineWidth: style.outlineWidth,
            outlineStyle: style.outlineStyle,
        };
    }""")
    assert focus == {
        "focused": True,
        "keyboardVisible": True,
        "outlineWidth": "2px",
        "outlineStyle": "solid",
    }, focus
    before_scroll = region.evaluate("element => element.scrollTop")
    page.keyboard.press("PageDown")
    page.wait_for_function("""before => {
        const target = document.querySelector('[data-review-mobile-region]');
        return target && document.activeElement === target && target.scrollTop > before;
    }""", arg=before_scroll, timeout=3000)
    after_scroll = region.evaluate("element => element.scrollTop")
    focus_retained = region.evaluate("element => document.activeElement === element")
    assert after_scroll > before_scroll, {
        "before_scroll": before_scroll,
        "after_scroll": after_scroll,
        "focus": focus,
    }
    assert focus_retained, {
        "before_scroll": before_scroll,
        "after_scroll": after_scroll,
        "focus_retained": focus_retained,
    }

    REPORT["bulk_review_mobile_layouts"].append({
        "viewport_width": geometry["viewportWidth"],
        "document_width": geometry["documentWidth"],
        "product_count": mobile_products.count(),
        "change_count": mobile_changes.count(),
        "exclude_button_count": mobile_exclusions.count(),
        "region_vertical_scroll": True,
        "region_horizontal_scroll": False,
        "region_scroll_width": geometry["regionScrollWidth"],
        "region_client_width": geometry["regionClientWidth"],
        "region_scroll_height": geometry["regionScrollHeight"],
        "region_client_height": geometry["regionClientHeight"],
        "scroll_top_before": before_scroll,
        "scroll_top_after": after_scroll,
        "focus_retained_after_pagedown": focus_retained,
        "touch_targets_at_least_44px": True,
        "layout_settle": settled,
    })
    check(
        "bulk_review_mobile_projection_exact_rows_identity_and_touch_targets",
        products=mobile_products.count(),
        changes=mobile_changes.count(),
        exclude_buttons=mobile_exclusions.count(),
        viewport_width=geometry["viewportWidth"],
        horizontal_overflow=False,
        touch_targets_at_least_44px=True,
    )
    check(
        "bulk_review_mobile_region_has_visible_keyboard_focus_and_scroll",
        focus_visible=focus["keyboardVisible"],
        keyboard_scroll_changed=after_scroll > before_scroll,
        focus_retained_after_pagedown=focus_retained,
        scroll_top_before=before_scroll,
        scroll_top_after=after_scroll,
    )
    interaction("320px_bulk_review_uses_exact_mobile_cards_with_keyboard_scroll")
    page.set_viewport_size({"width": 1280, "height": 900})
    wait_for_layout_settle(page, width=1280, theme="light")


def assert_bulk_selected_count(page, count: int) -> None:
    summary = page.locator("p").filter(
        has_text="Для характеристик выберите категорию",
    )
    assert summary.count() == 1, "Expected the bulk editor's selected-count summary"
    count_badge = summary.locator("span").first.inner_text()
    assert f"{count} выбрано" in " ".join(count_badge.split()), count_badge


def click_next_product_page(page, *, sort: str, expected_page: int = 2) -> None:
    """Click the visible next-page anchor and verify its full local query state."""
    links = page.locator('nav[aria-label="Навигация по страницам"] a[aria-label="Следующая"]')
    assert links.count() == 1, f"Expected exactly one next-page link, got {links.count()}"
    href = links.get_attribute("href")
    assert href, "Next-page link has no href"
    expected_query = {
        "search": ["Pipedream"],
        "brand": ["Pipedream"],
        "sort": [sort],
        "order": ["asc"],
        "page": [str(expected_page)],
        "per_page": ["50"],
    }
    _assert_local_products_href(href, expected_query)

    with page.expect_navigation(wait_until="domcontentloaded"):
        links.click()
    _assert_local_products_url(page.url, expected_query)
    check(
        "pagination_anchor_preserves_exact_filter_sort_and_page",
        expected_page=expected_page,
        sort=sort,
    )


def run_browser(app, fixture: dict[str, int], *, bulk_schema_race_only: bool = False) -> None:
    from seller_platform import app as seller_app
    from models import BulkEditHistory, CardEditHistory, Product, db

    assets = load_pinned_assets()
    origin = BASE
    with sync_playwright() as playwright:
        executable = (
            os.environ.get("UX01_CHROMIUM")
            or os.environ.get("UX01_BROWSER_CHROMIUM")
            or os.environ.get("OZON_BROWSER_CHROMIUM")
            or os.environ.get("CHROMIUM_BIN")
            or os.environ.get("WB_EDIT_CHROMIUM")
            or shutil.which("chromium")
            or "/opt/google/chrome/chrome"
        )
        browser = playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            service_workers="block",
        )
        allowed_local_posts = local_post_allowlist(fixture)
        context.route(
            "**/*",
            lambda route: bridge(
                route,
                assets=assets,
                origin=origin,
                allowed_local_posts=allowed_local_posts,
            ),
        )
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
        page.on("response", record_http_response)
        single_edit_post_requests = []
        browser_mutation_requests = []

        def record_single_edit_post(request):
            parsed = urlsplit(request.url)
            if (
                request.method == "POST"
                and BASE
                and f"{parsed.scheme}://{parsed.netloc}" == BASE
                and parsed.path == f"/products/{fixture['product_id']}/edit"
            ):
                single_edit_post_requests.append({
                    "method": request.method,
                    "path": parsed.path,
                })

        page.on("request", record_single_edit_post)

        def record_browser_mutation_attempt(request):
            if request.method not in {"GET", "HEAD"}:
                browser_mutation_requests.append({
                    "method": request.method,
                    "path": urlsplit(request.url).path,
                })

        page.on("request", record_browser_mutation_attempt)

        try:
            # Avoid the legacy dashboard's automatic WB analytics/finance reads;
            # authenticate directly to the page under test. Those APIs remain
            # blocked by forbidden_python_network everywhere in this fixture.
            page.goto(BASE + "/login?next=%2Fproducts", wait_until="domcontentloaded")
            page.locator('input[name="username"]').fill(USERNAME)
            page.locator('input[name="password"]').fill(PASSWORD)
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.locator('form button[type="submit"]').click()
            _assert_local_products_url(page.url, {})
            assert REPORT["provider_attempts"] == 0
            interaction("authenticated_login_with_csrf_enabled")

            if bulk_schema_race_only:
                page.goto(
                    BASE + "/products?search=Pipedream&brand=Pipedream"
                    "&sort=title&order=asc&page=1&per_page=50",
                    wait_until="domcontentloaded",
                )
                assert page.locator(".product-checkbox").count() == 50
                page.locator("#selectAll").check()
                page.get_by_role("button", name="Редактировать", exact=True).click()
                page.wait_for_url("**/products/bulk-edit")
                observations = assert_bulk_schema_load_races(page)
                check("bulk_characteristics_schema_latest_request_wins", **observations)
                interaction("deferred_schema_success_failure_and_context_switches_keep_current_editor_state")
                assert REPORT["provider_attempts"] == 0
                REPORT["status"] = "complete"
                return

            # Exercise a non-default page size before the existing exact-50
            # selection journey. Sorting must retain 100 and all meaningful
            # filters; only the five known blank controls from this form are
            # canonicalized by _assert_local_products_url.
            page.goto(
                BASE + "/products?search=Pipedream&brand=Pipedream"
                "&sort=vendor_code&order=asc&page=1&per_page=100",
                wait_until="domcontentloaded",
            )
            _assert_local_products_url(page.url, {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["vendor_code"], "order": ["asc"],
                "page": ["1"], "per_page": ["100"],
            })
            assert page.locator(".product-checkbox").count() == 51
            page.locator('select[name="sort"]').select_option("title")
            page.locator('select[name="order"]').select_option("asc")
            probe_form = page.locator('form[method="GET"][action="/products"]')
            with page.expect_request(
                lambda request: request.is_navigation_request()
                and urlsplit(request.url).path == "/products"
            ) as sort_request:
                with page.expect_navigation(wait_until="domcontentloaded"):
                    probe_form.locator('button[type="submit"]').click()
            assert sort_request.value.method == "GET"
            _assert_local_products_url(page.url, {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["title"], "order": ["asc"], "per_page": ["100"],
            })
            assert page.locator(".product-checkbox").count() == 51
            assert REPORT["provider_attempts"] == 0
            assert REPORT["fake_wb_client_instances"] == 0
            assert REPORT["fake_wb_write_calls"] == 0
            interaction("sort_form_preserves_nondefault_per_page_100_and_filters")

            list_url = (
                "/products?search=Pipedream&brand=Pipedream&sort=vendor_code"
                "&order=asc&page=1&per_page=50"
            )
            page.goto(BASE + list_url, wait_until="domcontentloaded")
            _assert_local_products_url(page.url, {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["vendor_code"], "order": ["asc"],
                "page": ["1"], "per_page": ["50"],
            })
            assert page.locator(".product-checkbox").count() == 50
            assert_keyboard_focus(page, "products_list")
            page.locator("#selectAll").check()
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            interaction("select_exact_first_page_50")

            foreign_probe = page.evaluate("""async payload => {
                const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const response = await fetch('/products/selection/resolve', {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                    body: JSON.stringify(payload),
                });
                const body = await response.json();
                return {status: response.status, code: body.code};
            }""", {
                "mode": "ids",
                "ids": [fixture["foreign_product_id"]],
                "filters": {"search": "Pipedream", "brand": "Pipedream"},
                "sort": "vendor_code", "order": "asc", "page": 1,
                "per_page": 50,
                "return_to": list_url,
            })
            assert foreign_probe == {"status": 403, "code": "selection_unavailable"}, foreign_probe
            REPORT["checks"].append({
                "name": "foreign_product_id_rejected_before_provider",
                "status": "passed",
                "http_status": foreign_probe["status"],
                "code": foreign_probe["code"],
            })

            click_next_product_page(page, sort="vendor_code")
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            assert page.locator(".product-checkbox").count() == 1
            interaction("selection_survives_cross_page_navigation")

            page.locator('select[name="sort"]').select_option("title")
            page.locator('select[name="order"]').select_option("asc")
            filter_form = page.locator('form[method="GET"][action="/products"]')
            assert filter_form.count() == 1, f"Expected one catalog filter form, got {filter_form.count()}"
            with page.expect_request(
                lambda request: request.is_navigation_request()
                and urlsplit(request.url).path == "/products"
            ) as sort_request:
                with page.expect_navigation(wait_until="domcontentloaded"):
                    filter_form.locator('button[type="submit"]').click()
            assert sort_request.value.method == "GET"
            _assert_local_products_url(page.url, {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["title"], "order": ["asc"], "per_page": ["50"],
            })
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            click_next_product_page(page, sort="title")
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            interaction("selection_survives_sort_and_second_page")
            capture_layout(page, "products_list")

            category_probe = page.evaluate("""async names => {
                const values = {};
                for (const name of names) {
                    const response = await fetch('/api/characteristics/' + encodeURIComponent(name), {credentials: 'same-origin'});
                    const body = await response.json();
                    values[name] = {
                        status: response.status,
                        subject_id: body.subject_id,
                        count: body.count,
                        characteristic_count: body.characteristics?.length ?? 0,
                        schema_source: body.schema_source,
                        provider_io: body.provider_io,
                    };
                }
                return values;
            }""", ["Свечи эротик", "Мастурбаторы мужские", "Unmapped synthetic category"])
            assert category_probe["Свечи эротик"] == {
                "status": 200,
                "subject_id": 5880,
                "count": 31,
                "characteristic_count": 31,
                "schema_source": "local_authoritative_cache",
                "provider_io": False,
            }, category_probe
            assert category_probe["Мастурбаторы мужские"] == {
                "status": 200,
                "subject_id": 5070,
                "count": 31,
                "characteristic_count": 31,
                "schema_source": "local_authoritative_cache",
                "provider_io": False,
            }, category_probe
            assert category_probe["Unmapped synthetic category"]["status"] == 409
            assert category_probe["Unmapped synthetic category"]["provider_io"] is False
            check(
                "exact_subject_cache_and_unmapped_category_fail_closed",
                subject_id=5880,
                working_subject_id=5070,
                unmapped_status=category_probe["Unmapped synthetic category"]["status"],
            )

            # Exercise the actual mixed-category bulk editor with a normal
            # two-row selection. Only the selected Wildberries category may
            # appear in the preview; the other category remains an explicit
            # skip and preview must not cross the fake provider boundary.
            two_subject_context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            two_subject_context.route(
                "**/*",
                lambda route: bridge(
                    route,
                    assets=assets,
                    origin=origin,
                    allowed_local_posts=allowed_local_posts,
                ),
            )
            two_subject_page = two_subject_context.new_page()
            two_subject_page.set_default_timeout(15000)
            two_subject_page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
            two_subject_page.on("response", record_http_response)
            try:
                two_subject_login = authenticate_isolated_browser_session(
                    two_subject_page,
                    username=USERNAME,
                    next_path="/products",
                )
                assert two_subject_login == {
                    "status": 200,
                    "path": "/products",
                    "redirected": True,
                }, two_subject_login
                two_subject_page.goto(
                    BASE + "/products?sort=title&order=asc&page=1&per_page=100",
                    wait_until="domcontentloaded",
                )
                selected_subject_ids = [
                    fixture["product_id"],
                    fixture["male_subject_product_id"],
                ]
                visible_product_ids = {
                    int(value)
                    for value in two_subject_page.locator(".product-checkbox").evaluate_all(
                        "inputs => inputs.map(input => input.value)",
                    )
                }
                assert set(selected_subject_ids) <= visible_product_ids, {
                    "selected": selected_subject_ids,
                    "visible": sorted(visible_product_ids),
                }
                for product_id in selected_subject_ids:
                    two_subject_page.locator(
                        f'.product-checkbox[value="{product_id}"]',
                    ).check()
                two_subject_page.wait_for_function(
                    "() => document.querySelector('#selectedCount')?.textContent?.trim() === '2'",
                    timeout=5000,
                )
                exact_selected_ids = two_subject_page.locator(
                    '#bulkActionForm input[name="product_ids"]',
                ).evaluate_all("inputs => inputs.map(input => Number(input.value))")
                assert sorted(exact_selected_ids) == sorted(selected_subject_ids), exact_selected_ids
                two_subject_page.get_by_role(
                    "button", name="Редактировать", exact=True,
                ).click()
                two_subject_page.wait_for_url("**/products/bulk-edit")
                assert_bulk_selected_count(two_subject_page, 2)
                two_subject_page.locator(
                    'input[name="operation"][value="update_characteristic"]',
                ).check()
                assert "поля разных категорий не смешиваются." in two_subject_page.locator("body").inner_text()
                other_category_buttons = two_subject_page.get_by_role("button").filter(
                    has_text="Мастурбаторы мужские",
                )
                assert other_category_buttons.count() == 1
                selected_category_buttons = two_subject_page.get_by_role("button").filter(
                    has_text="Свечи эротик",
                )
                assert selected_category_buttons.count() == 1
                category_button = selected_category_buttons.first
                # Ignore the initial blank-category schema read triggered by
                # opening this operation; wait for the exact user choice.
                with two_subject_page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and urlsplit(response.url).path == "/api/characteristics/multi-category"
                        and json.loads(response.request.post_data or "{}").get("subject_ids") == [5880]
                    ),
                    timeout=5000,
                ) as category_schema_response:
                    category_button.click()
                assert category_schema_response.value.status == 200
                category_schema_request = json.loads(
                    category_schema_response.value.request.post_data or "{}",
                )
                assert sorted(category_schema_request.get("product_ids", [])) == sorted(selected_subject_ids)
                assert category_schema_request.get("subject_ids") == [5880]
                selected_category_copy = two_subject_page.locator("body").inner_text()
                assert (
                    "Категория Свечи эротик: 1 из 2 выбранных товаров. "
                    "Другие категории не изменятся."
                ) in selected_category_copy

                characteristic_select = two_subject_page.locator(
                    'select[x-model="addingCharId"]:visible',
                )
                assert characteristic_select.count() == 1
                characteristic_select.select_option("101")
                value_field = two_subject_page.locator(
                    'div[x-show="addingChar"] input[placeholder="Введите значение"]:visible',
                )
                value_field.wait_for(state="visible", timeout=5000)
                assert value_field.count() == 1
                value_field.fill("Mixed category preview only")
                two_subject_page.get_by_role(
                    "button", name="Добавить к изменениям", exact=True,
                ).click()

                fake_clients_before_preview = REPORT["fake_wb_client_instances"]
                fake_writes_before_preview = REPORT["fake_wb_write_calls"]
                with two_subject_page.expect_response(
                    lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == "/products/bulk-edit",
                    timeout=5000,
                ) as two_subject_preview_response:
                    two_subject_page.locator(
                        'form[action="/products/bulk-edit"] button[type="submit"]:visible',
                    ).click()
                assert two_subject_preview_response.value.status == 200
                two_subject_page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
                two_subject_counts = two_subject_page.locator(
                    "section[aria-label='Сводка предпросмотра'] div.sh-card",
                ).evaluate_all("""cards => Object.fromEntries(cards.map(card => [
                    card.querySelector('p.text-xs').textContent.trim(),
                    Number(card.querySelector('p.text-2xl').textContent.trim()),
                ]))""")
                assert two_subject_counts == {
                    "Выбрано": 2,
                    "Подходит для операции": 1,
                    "Изменится": 1,
                    "Пропущено": 1,
                    "Ошибки": 0,
                }, two_subject_counts
                assert "Для этой категории подходят 1 из 2 выбранных карточек." in two_subject_page.locator("body").inner_text()
                assert two_subject_page.locator("section.sh-card--flush tbody tr").count() == 1
                skipped_section = two_subject_page.locator("details.sh-card").filter(
                    has_text="Пропущено: 1",
                )
                assert skipped_section.count() == 1
                skipped_section.locator("summary").click()
                skipped_text = skipped_section.inner_text()
                assert "MALE-SUBJECT-FIXTURE" in skipped_text
                assert "Карточка относится к другой категории Wildberries" in skipped_text
                assert "subjectID" not in skipped_text
                assert REPORT["fake_wb_client_instances"] == fake_clients_before_preview
                assert REPORT["fake_wb_write_calls"] == fake_writes_before_preview
                check(
                    "mixed_subject_bulk_preview_uses_exact_selection_and_skips_other_category",
                    selected_ids=sorted(selected_subject_ids),
                    category_ids=category_schema_request.get("subject_ids"),
                    counts=two_subject_counts,
                    skip_reason="Карточка относится к другой категории Wildberries",
                    fake_provider_writes_before_confirmation=0,
                )
                interaction(
                    "two_product_mixed_category_preview_selects_one_category_without_writes",
                    selected=2,
                    eligible=1,
                    skipped=1,
                )
            finally:
                two_subject_context.close()

            page.get_by_role("button", name="Редактировать", exact=True).click()
            page.wait_for_url("**/products/bulk-edit")
            account_summary = page.locator("p.uppercase.tracking-wide")
            assert account_summary.count() == 1, (
                f"Expected one scoped WB account/channel label, got {account_summary.count()}"
            )
            account_summary_text = " ".join(account_summary.inner_text().casefold().split())
            assert account_summary_text.startswith("аккаунт: wb edit synthetic shop"), account_summary_text
            assert "wb account wb-edit-account-synthetic" in account_summary_text, account_summary_text
            assert account_summary_text.endswith("маркетплейс: wildberries"), account_summary_text
            assert_bulk_selected_count(page, 50)
            assert "Pipedream" in page.locator("body").inner_text()
            assert_keyboard_focus(page, "bulk_editor")
            schema_race_observations = assert_bulk_schema_load_races(page)
            check(
                "bulk_characteristics_schema_latest_request_wins",
                **schema_race_observations,
            )
            interaction("deferred_schema_success_failure_and_context_switches_keep_current_editor_state")
            return_links = page.locator('nav a[href^="/products?"]')
            assert return_links.count() == 1, f"Expected one safe products return link, got {return_links.count()}"
            return_href = return_links.get_attribute("href")
            assert return_href, "Safe products return link has no href"
            expected_return_query = {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["title"], "order": ["asc"],
                "page": ["2"], "per_page": ["50"],
            }
            _assert_local_products_href(return_href, expected_return_query)
            assert_bulk_editor_mobile_footer(page, expected_return_query)
            assert REPORT["fake_wb_client_instances"] == 0
            capture_layout(page, "bulk_editor")
            interaction("bulk_editor_names_wb_account_channel_and_safe_return_context")

            with page.expect_navigation(wait_until="domcontentloaded"):
                return_links.click()
            _assert_local_products_url(page.url, expected_return_query)
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            assert page.locator('select[name="sort"]').input_value() == "title"
            interaction("safe_return_link_restores_filter_sort_page_selection")
            page.get_by_role("button", name="Редактировать", exact=True).click()
            page.wait_for_url("**/products/bulk-edit")

            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Synthetic mixed action must stop")
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + "/products/bulk-edit",
                timeout=10000,
            ) as mixed_manual_ai_response:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
                    page.locator('form[action="/products/bulk-edit"]').evaluate("""form => {
                        const hidden = document.createElement('input');
                        hidden.type = 'hidden'; hidden.name = 'ai_operations'; hidden.value = 'ai_keywords';
                        form.appendChild(hidden);
                        form.submit();
                    }""")
            response = mixed_manual_ai_response.value
            assert response.status == 200
            assert response.request.method == "POST"
            assert response.url == BASE + "/products/bulk-edit"
            assert "Ручная и AI-операции не объединяются" in page.locator("body").inner_text()
            assert "Ничего не было применено" in page.locator("body").inner_text()
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check("mixed_manual_and_ai_operations_blocked_before_write")
            interaction("mixed_manual_ai_post_gets_actionable_no_write_notice")

            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Pipedream")
            page.locator('form[action="/products/bulk-edit"] button[type="submit"]').click()
            page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(page, changed="0")
            assert page.locator('input[name="preview_token"]').input_value() == ""
            assert page.get_by_role("button", name="Подтвердить и применить 0 карточек").is_disabled()
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check("no_op_bulk_preview_has_no_apply_token_or_provider_client")
            interaction("no_op_preview_has_no_apply_token_or_provider_client")

            page.locator("nav button").filter(has_text="Массовое редактирование").click()
            page.wait_for_url("**/products/bulk-edit")
            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Synthetic reviewed brand")
            page.locator('form[action="/products/bulk-edit"] button[type="submit"]').click()
            page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(page, changed="50")
            assert "Pipedream" in page.content() and "Synthetic reviewed brand" in page.content()
            reviewed_identity = page.locator(
                f'[data-review-product-id="{fixture["product_id"]}"]',
            )
            assert reviewed_identity.count() == 1
            assert "Pipedream Synthetic Product 000" in reviewed_identity.inner_text()
            assert "Артикул: PD-000" in reviewed_identity.inner_text()
            assert f"ID Seller Hub: {fixture['product_id']}" in reviewed_identity.inner_text()
            assert "nmID WB: 900000" in reviewed_identity.inner_text()
            assert reviewed_identity.locator("img").get_attribute("src") == (
                "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
            )
            from models import Product, db
            with seller_app.app_context():
                expected_sync = db.session.get(Product, fixture["product_id"]).last_sync
            assert reviewed_identity.locator("time").get_attribute("datetime") == (
                expected_sync.isoformat() + "Z"
            )
            assert reviewed_identity.locator("time").inner_text() == (
                expected_sync.strftime("%d.%m.%Y %H:%M UTC")
            )
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check(
                "bulk_review_shows_exact_local_product_identity_photo_and_sync_time",
                product_id=fixture["product_id"],
                wb_nm_id=900000,
                sync_time_observed=True,
                provider_io=0,
            )
            assert_bulk_review_mobile_projection(page, fixture, expected_sync)
            assert_keyboard_focus(page, "bulk_review")
            check(
                "manual_preview_reports_exact_selection_diff_before_fake_apply",
                selected=50,
                changed=50,
                skipped=0,
                fake_wb_client_instances=0,
            )
            interaction("manual_bulk_preview_shows_exact_50_row_diff_without_provider_io")
            capture_layout(page, "bulk_review")

            # Simulate a local edit racing the confirmation. The signed review
            # must be rejected before the fake provider is even constructed.
            from models import Product, db
            with seller_app.app_context():
                first_product = db.session.get(Product, fixture["product_id"])
                first_product.brand = "Concurrent local change"
                db.session.commit()
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.get_by_role("button", name="Подтвердить и применить 50 карточек").click()
            _assert_local_products_url(page.url, expected_return_query)
            assert "Точный выбор изменился или больше не соответствует фильтрам" in page.locator("body").inner_text()
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            expected_selection_ids = list(range(fixture["product_id"], fixture["product_id"] + 50))
            restored_selection_ids = page.locator(
                '#bulkActionForm input[name="product_ids"]',
            ).evaluate_all("inputs => inputs.map(input => Number(input.value))")
            assert sorted(restored_selection_ids) == expected_selection_ids, {
                "expected_count": len(expected_selection_ids),
                "actual_count": len(restored_selection_ids),
            }
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check(
                "filter_drift_returns_to_signed_context_and_preserves_exact_selection",
                selected=50,
                safe_return_query=expected_return_query,
                fake_wb_client_instances=REPORT["fake_wb_client_instances"],
            )
            interaction("filter_drift_notice_keeps_safe_context_and_exact_50_id_set")

            with seller_app.app_context():
                first_product = db.session.get(Product, fixture["product_id"])
                first_product.brand = "Pipedream"
                db.session.commit()
            page.reload(wait_until="domcontentloaded")
            _assert_local_products_url(page.url, expected_return_query)
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            restored_selection_ids = page.locator(
                '#bulkActionForm input[name="product_ids"]',
            ).evaluate_all("inputs => inputs.map(input => Number(input.value))")
            assert sorted(restored_selection_ids) == expected_selection_ids
            page.get_by_role("button", name="Редактировать", exact=True).click()
            page.wait_for_url("**/products/bulk-edit")
            assert_bulk_selected_count(page, 50)
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check("restored_filter_allows_exact_50_to_reopen_through_ui", selected=50)
            interaction("restored_filter_reopens_exact_50_product_selection_through_ui")

            # Exercise content-fingerprint drift independently of filter drift:
            # description changes do not alter the signed brand filter.
            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Synthetic reviewed brand")
            page.locator('form[action="/products/bulk-edit"] button[type="submit"]').click()
            page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(page, changed="50")
            with seller_app.app_context():
                first_product = db.session.get(Product, fixture["product_id"])
                original_description = first_product.description
                first_product.description = "Concurrent local description drift"
                db.session.commit()
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.get_by_role("button", name="Подтвердить и применить 50 карточек").click()
            page.wait_for_url("**/products/bulk-edit")
            assert "Карточка или схема WB изменилась после предпросмотра; проверьте снова" in page.locator("body").inner_text()
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check("description_fingerprint_drift_rejected_before_provider", selected=50)
            interaction("non_filter_description_drift_rejected_without_provider_io")
            with seller_app.app_context():
                first_product = db.session.get(Product, fixture["product_id"])
                first_product.description = original_description
                db.session.commit()

            # A fresh review after the local description is restored is the
            # only path to the fake provider boundary.
            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Synthetic reviewed brand")
            page.locator('form[action="/products/bulk-edit"] button[type="submit"]').click()
            page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(page, changed="50")
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check("fresh_preview_after_description_restore_reports_exact_selection", selected=50)
            interaction("fresh_review_created_after_restoring_local_description")

            with page.expect_navigation(wait_until="domcontentloaded"):
                page.get_by_role("button", name="Подтвердить и применить 50 карточек").click()
            _assert_local_products_url(page.url, expected_return_query)
            assert "Успешно обновлено товаров: 50" in page.locator("body").inner_text()
            assert REPORT["fake_wb_write_calls"] == 1
            assert len(REPORT["fake_wb_written_products"]) == 50
            assert len(set(REPORT["fake_wb_written_products"])) == 50
            assert sorted(REPORT["fake_wb_written_products"]) == list(range(900000, 900050))
            assert REPORT["provider_attempts"] == 0

            expected_product_ids = list(range(
                fixture["product_id"], fixture["product_id"] + 50,
            ))
            with seller_app.app_context():
                history = BulkEditHistory.query.filter_by(
                    seller_id=fixture["seller_id"],
                    operation_type="update_brand",
                ).order_by(BulkEditHistory.id.desc()).first()
                assert history is not None
                history_id = int(history.id)
                assert history.status == "completed"
                assert history.total_products == 50
                assert history.success_count == 50
                assert history.error_count == 0
                summary = (history.operation_params or {}).get("review_summary") or {}
                assert summary.get("selected") == 50
                assert summary.get("eligible") == 50
                assert summary.get("changed") == 50
                assert summary.get("skipped") == 0
                assert summary.get("errors") == 0
                assert summary.get("changed_product_ids") == expected_product_ids
                saved_rows = CardEditHistory.query.filter_by(
                    seller_id=fixture["seller_id"],
                    bulk_edit_id=history_id,
                ).order_by(CardEditHistory.id.asc()).all()
                assert [int(row.product_id) for row in saved_rows] == expected_product_ids

            history_nav_links = page.locator(
                '.sh-page-actions a[href="/bulk-history"]',
            )
            assert history_nav_links.count() == 1
            assert history_nav_links.is_visible()
            with page.expect_navigation(wait_until="domcontentloaded"):
                history_nav_links.first.click()
            history_url = urlsplit(page.url)
            assert (
                history_url.scheme == urlsplit(BASE).scheme
                and history_url.netloc == urlsplit(BASE).netloc
                and history_url.path == "/bulk-history"
                and not history_url.query and not history_url.fragment
            ), {"path": history_url.path}
            detail_link = page.locator(f'a[href="/bulk-history/{history_id}"]')
            assert detail_link.count() == 1
            with page.expect_navigation(wait_until="domcontentloaded"):
                detail_link.click()
            detail_url = urlsplit(page.url)
            assert (
                detail_url.scheme == urlsplit(BASE).scheme
                and detail_url.netloc == urlsplit(BASE).netloc
                and detail_url.path == f"/bulk-history/{history_id}"
                and not detail_url.query and not detail_url.fragment
            ), {"path": detail_url.path}
            assert "Изменённые товары (50)" in page.locator("body").inner_text()
            detail_product_ids = page.locator(
                "[data-operations-product-id]",
            ).evaluate_all(
                "rows => rows.map(row => Number(row.dataset.operationsProductId))",
            )
            assert sorted(detail_product_ids) == expected_product_ids
            assert len(detail_product_ids) == 50
            assert page.locator('[data-operations-changed-field="brand"]').count() == 50
            check(
                "successful_exact_50_apply_is_seller_scoped_and_readable_in_history",
                selected=50,
                history_rows=len(detail_product_ids),
                fake_wb_write_calls=REPORT["fake_wb_write_calls"],
            )
            interaction("successful_apply_returns_to_context_then_opens_own_history_detail")
            check(
                "reviewed_apply_reaches_only_fake_provider_with_exact_50_products",
                fake_wb_write_calls=REPORT["fake_wb_write_calls"],
                exact_product_count=len(REPORT["fake_wb_written_products"]),
            )
            interaction("single_review_confirm_reaches_only_fake_provider_boundary")

            # Isolate legacy core values after the earlier bulk scenario. A
            # characteristic-only edit must preserve nullable values and the
            # exact stored title without asking the seller to repair the form.
            with seller_app.app_context():
                provider_card = FakeWBClient._read_full_card(900000)
                pre_single_fake_core_values = {
                    field: provider_card.get(field)
                    for field in ("vendorCode", "title", "description", "brand")
                }
                single_product = Product.query.filter_by(
                    id=fixture["product_id"], seller_id=fixture["seller_id"],
                ).one()
                pre_single_core_values = {
                    field: getattr(single_product, field)
                    for field in ("title", "brand", "description")
                }
                single_product.vendor_code = "PD-000"
                single_product.title = "  Pipedream Synthetic Product 000  "
                single_product.description = None
                single_product.brand = None
                provider_card.update({
                    "vendorCode": single_product.vendor_code or "",
                    "title": single_product.title,
                    "description": single_product.description,
                    "brand": single_product.brand,
                })
                _FAKE_WB_CARDS[900000] = FakeWBClient._copy_card(provider_card)
                db.session.commit()

            single_edit_path = f"/products/{fixture['product_id']}/edit"
            page.goto(BASE + single_edit_path, wait_until="domcontentloaded")
            assert_keyboard_focus(page, "single_product_edit")
            characteristics_panel = page.locator("section[aria-labelledby='wb-characteristics-title']")
            assert "Свечи эротик" in characteristics_panel.inner_text()
            assert "subjectID 5880" not in characteristics_panel.inner_text()
            assert "4 заполнено · 31 в схеме" in characteristics_panel.inner_text()
            assert page.locator("#char_202").count() == 0
            assert page.locator("#char_303").get_attribute("type") == "number"
            assert "Единица: г" in page.locator('label[for="char_303"]').inner_text()
            assert page.locator("#char_404").get_attribute("multiple") is not None
            assert page.locator("#char_404 option").count() == 3
            assert page.locator("#char_303").input_value() == "125"
            assert page.locator("#char_404 option:checked").evaluate_all(
                "options => options.map(option => option.value)"
            ) == ["Пластик", "Металл"]
            assert page.locator("#char_502").evaluate("el => el.tagName === 'TEXTAREA'")
            assert page.locator("#char_502").input_value() == "Existing line one\nExisting line two"
            assert page.locator("#char_506").input_value() == ""
            assert "Сохранено" in page.locator('label[for="char_506"]').inner_text()
            assert "SYNTHETIC-WB-SKU-000" in page.locator("body").inner_text()
            assert page.get_by_text("Размеры и штрихкоды", exact=False).count() == 1
            size_id_headers = page.locator("table thead th").filter(
                has_text=re.compile(r"^ID размера Wildberries$", re.IGNORECASE),
            )
            assert size_id_headers.count() == 1
            assert " ".join(size_id_headers.first.inner_text().split()).casefold() == "id размера wildberries"
            assert page.locator('input[name="sku"], input[name="sizes_json"]').count() == 0

            legacy_details = characteristics_panel.locator("details")
            assert legacy_details.count() == 1
            legacy_summary = legacy_details.locator("summary")
            assert "только просмотр" in legacy_summary.inner_text().lower()
            legacy_summary.focus()
            page.keyboard.press("Enter")
            page.wait_for_function("document.querySelector('section[aria-labelledby=\\\"wb-characteristics-title\\\"] details')?.open === true")
            legacy_details_text = legacy_details.inner_text()
            stale_read_only_observation = {
                "field_id": 501,
                "disclosure_opened_by_keyboard": True,
                "field_text_present": "Stale prior-subject field" in legacy_details_text,
                "saved_value_present": "Preserve read-only historical value" in legacy_details_text,
                "form_control_count": legacy_details.locator("input,select,textarea").count(),
            }
            assert stale_read_only_observation == {
                "field_id": 501,
                "disclosure_opened_by_keyboard": True,
                "field_text_present": True,
                "saved_value_present": True,
                "form_control_count": 0,
            }, stale_read_only_observation
            page.keyboard.press("Enter")
            page.wait_for_function("document.querySelector('section[aria-labelledby=\\\"wb-characteristics-title\\\"] details')?.open === false")

            page.set_viewport_size({"width": 390, "height": 900})
            page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
            wait_for_layout_settle(page, width=390, theme="light")
            initial_progressive_view = page.evaluate("""() => {
                const root = document.querySelector('[x-data^="productEditApp"]');
                const form = root?.querySelector('form.space-y-6');
                const cards = Array.from(root?.querySelectorAll('[data-wb-characteristic-field-id]') || []);
                const picker = root?.querySelector('#wb-optional-characteristic-picker');
                const rect = element => {
                    const box = element?.getBoundingClientRect();
                    return box ? {width: Math.round(box.width), height: Math.round(box.height)} : null;
                };
                const schema = JSON.parse(root?.querySelector('#wb-characteristic-schema')?.textContent || '[]');
                const byId = id => cards.find(card => Number(card.dataset.wbCharacteristicFieldId) === id);
                const requiredCard = byId(500);
                const label = root?.querySelector('.wb-characteristics-filled-count')?.innerText || '';
                return {
                    viewport_width: innerWidth,
                    document_width: document.documentElement.scrollWidth,
                    schema_field_count: schema.length,
                    visible_field_ids: cards.map(card => Number(card.dataset.wbCharacteristicFieldId)).sort((a,b) => a-b),
                    saved_text_input: root?.querySelector('#char_101')?.tagName === 'INPUT',
                    numeric_grams_input: root?.querySelector('#char_303')?.type === 'number'
                        && root?.querySelector('#char_303')?.value === '125',
                    dictionary_multiple_select: root?.querySelector('#char_404')?.multiple === true,
                    bounded_textarea: root?.querySelector('#char_502')?.tagName === 'TEXTAREA'
                        && root?.querySelector('#char_502')?.value === 'Existing line one\\nExisting line two',
                    present_empty_visible: !!byId(506)
                        && byId(506).innerText.includes('Сохранено')
                        && root?.querySelector('#char_506')?.value === '',
                    present_empty_excluded_from_picker: !Array.from(picker?.options || [])
                        .some(option => option.value === '506'),
                    filled_summary: label,
                    country_hidden_until_chosen: !root?.querySelector('#char_202'),
                    picker_country_label: Array.from(picker?.options || [])
                        .find(option => option.value === '202')?.textContent.trim() || '',
                    optional_choice_count: Array.from(picker?.options || []).filter(option => option.value).length,
                    required_missing_visible: !!requiredCard
                        && requiredCard.innerText.includes('Не заполнено · обязательное'),
                    required_missing_input_visible: !!requiredCard?.querySelector('#char_500'),
                    has_changes: !!window.Alpine?.$data(root).hasChanges,
                    save_disabled: !!form?.querySelector('button[type="submit"]')?.disabled,
                    picker_box: rect(picker),
                    add_button_box: rect(root?.querySelector('#wb-add-optional-characteristic')),
                };
            }""")
            assert initial_progressive_view["viewport_width"] == 390, initial_progressive_view
            assert initial_progressive_view["document_width"] <= 390, initial_progressive_view
            assert initial_progressive_view["schema_field_count"] == 31, initial_progressive_view
            assert initial_progressive_view["visible_field_ids"] == [101, 303, 404, 500, 502, 506], initial_progressive_view
            assert len(initial_progressive_view["visible_field_ids"]) < initial_progressive_view["schema_field_count"], initial_progressive_view
            assert initial_progressive_view["saved_text_input"] is True, initial_progressive_view
            assert initial_progressive_view["numeric_grams_input"] is True, initial_progressive_view
            assert initial_progressive_view["dictionary_multiple_select"] is True, initial_progressive_view
            assert initial_progressive_view["bounded_textarea"] is True, initial_progressive_view
            assert initial_progressive_view["present_empty_visible"] is True, initial_progressive_view
            assert initial_progressive_view["present_empty_excluded_from_picker"] is True, initial_progressive_view
            assert initial_progressive_view["filled_summary"] == "4 заполнено · 31 в схеме", initial_progressive_view
            assert initial_progressive_view["country_hidden_until_chosen"] is True, initial_progressive_view
            assert initial_progressive_view["picker_country_label"] == "Страна производства", initial_progressive_view
            assert initial_progressive_view["optional_choice_count"] == 25, initial_progressive_view
            assert initial_progressive_view["required_missing_visible"] is True, initial_progressive_view
            assert initial_progressive_view["required_missing_input_visible"] is True, initial_progressive_view
            assert stale_read_only_observation["disclosure_opened_by_keyboard"] is True, stale_read_only_observation
            assert initial_progressive_view["has_changes"] is False, initial_progressive_view
            assert initial_progressive_view["save_disabled"] is True, initial_progressive_view
            assert initial_progressive_view["picker_box"]["height"] >= 44, initial_progressive_view
            assert initial_progressive_view["add_button_box"]["height"] >= 44, initial_progressive_view

            def add_optional_field_with_keyboard(field_id: int) -> dict:
                picker = page.locator("#wb-optional-characteristic-picker")
                picker.focus()
                page.keyboard.press("ArrowDown")
                page.wait_for_function(
                    "id => document.querySelector('#wb-optional-characteristic-picker')?.value === String(id)",
                    arg=field_id,
                )
                picker_keyboard_focus = page.evaluate("""() => ({
                    id: document.activeElement?.id || '',
                    focus_visible: !!document.activeElement?.matches(':focus-visible'),
                    selected_value: document.querySelector('#wb-optional-characteristic-picker')?.value || '',
                })""")
                assert picker_keyboard_focus["id"] == "wb-optional-characteristic-picker", picker_keyboard_focus
                assert picker_keyboard_focus["selected_value"] == str(field_id), picker_keyboard_focus
                # Keep the native select closed. Enter opens Chromium's native
                # option popup; Tab can then remain inside that popup instead
                # of moving to the page's Add button.
                page.keyboard.press("Tab")
                add_button_focus = page.evaluate("""() => ({
                    text: document.activeElement?.innerText?.trim() || '',
                    focus_visible: !!document.activeElement?.matches(':focus-visible'),
                    box_height: document.activeElement?.getBoundingClientRect().height || 0,
                })""")
                assert add_button_focus["text"] == "Добавить поле", add_button_focus
                assert add_button_focus["focus_visible"] is True, add_button_focus
                assert add_button_focus["box_height"] >= 44, add_button_focus
                page.keyboard.press("Enter")
                page.wait_for_function(
                    "id => !!document.getElementById('char_' + id)",
                    arg=field_id,
                )
                added_focus = page.evaluate("""id => ({
                    active_id: document.activeElement?.id || '',
                    field_tag: document.getElementById('char_' + id)?.tagName || '',
                    focus_visible: !!document.activeElement?.matches(':focus-visible'),
                })""", field_id)
                assert added_focus["active_id"] == f"char_{field_id}", added_focus
                return {
                    "picker_focus": picker_keyboard_focus,
                    "add_button_focus": add_button_focus,
                    "added_control_focus": added_focus,
                }

            empty_attempt_before = {
                "post_count": len(single_edit_post_requests),
                "fake_client_instances": REPORT["fake_wb_client_instances"],
                "fake_single_write_calls": REPORT["fake_wb_single_write_calls"],
                "provider_attempts": REPORT["provider_attempts"],
            }
            empty_add_keyboard = add_optional_field_with_keyboard(202)
            assert page.locator("#char_202").evaluate("el => el.tagName === 'SELECT'")
            empty_field_tag = page.locator("#char_202").evaluate("el => el.tagName")
            assert empty_field_tag == "SELECT", empty_field_tag
            page.locator("#title").focus()
            page.keyboard.press("Enter")
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            empty_attempt_after_enter = {
                "still_on_edit_route": urlsplit(page.url).path == single_edit_path,
                "has_changes": page.evaluate(
                    "root => Alpine.$data(root).hasChanges",
                    arg=page.locator('[x-data^="productEditApp"]').element_handle(),
                ),
                "save_disabled": page.locator('form.space-y-6 button[type="submit"]').is_disabled(),
                "post_count": len(single_edit_post_requests),
                "fake_client_instances": REPORT["fake_wb_client_instances"],
                "fake_single_write_calls": REPORT["fake_wb_single_write_calls"],
                "provider_attempts": REPORT["provider_attempts"],
            }
            assert empty_attempt_after_enter["still_on_edit_route"] is True, empty_attempt_after_enter
            assert empty_attempt_after_enter["has_changes"] is False, empty_attempt_after_enter
            assert empty_attempt_after_enter["save_disabled"] is True, empty_attempt_after_enter
            assert empty_attempt_after_enter["post_count"] == empty_attempt_before["post_count"], empty_attempt_after_enter
            assert empty_attempt_after_enter["fake_client_instances"] == empty_attempt_before["fake_client_instances"], empty_attempt_after_enter
            assert empty_attempt_after_enter["fake_single_write_calls"] == empty_attempt_before["fake_single_write_calls"], empty_attempt_after_enter
            assert empty_attempt_after_enter["provider_attempts"] == empty_attempt_before["provider_attempts"] == 0, empty_attempt_after_enter

            empty_request_submit = page.evaluate("""() => {
                const form = document.querySelector('form.space-y-6');
                let observed = {seen: false, default_prevented: false};
                form.addEventListener('submit', event => {
                    observed = {seen: true, default_prevented: event.defaultPrevented};
                }, {once: true});
                form.requestSubmit();
                return observed;
            }""")
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            empty_attempt_after_request_submit = {
                "submit_event": empty_request_submit,
                "still_on_edit_route": urlsplit(page.url).path == single_edit_path,
                "post_count": len(single_edit_post_requests),
                "fake_client_instances": REPORT["fake_wb_client_instances"],
                "fake_single_write_calls": REPORT["fake_wb_single_write_calls"],
                "provider_attempts": REPORT["provider_attempts"],
            }
            assert empty_attempt_after_request_submit["submit_event"] == {
                "seen": True,
                "default_prevented": True,
            }, empty_attempt_after_request_submit
            assert empty_attempt_after_request_submit["still_on_edit_route"] is True, empty_attempt_after_request_submit
            assert empty_attempt_after_request_submit["post_count"] == empty_attempt_before["post_count"], empty_attempt_after_request_submit
            assert empty_attempt_after_request_submit["fake_client_instances"] == empty_attempt_before["fake_client_instances"], empty_attempt_after_request_submit
            assert empty_attempt_after_request_submit["fake_single_write_calls"] == empty_attempt_before["fake_single_write_calls"], empty_attempt_after_request_submit
            assert empty_attempt_after_request_submit["provider_attempts"] == empty_attempt_before["provider_attempts"] == 0, empty_attempt_after_request_submit

            page.locator("#char_202").focus()
            page.keyboard.press("Tab")
            remove_button_focus = page.evaluate("""() => ({
                label: document.activeElement?.getAttribute('aria-label') || '',
                focus_visible: !!document.activeElement?.matches(':focus-visible'),
                box_height: document.activeElement?.getBoundingClientRect().height || 0,
            })""")
            assert remove_button_focus["label"] == "Убрать пустое поле «Страна производства»", remove_button_focus
            assert remove_button_focus["focus_visible"] is True, remove_button_focus
            assert remove_button_focus["box_height"] >= 44, remove_button_focus
            page.keyboard.press("Enter")
            page.wait_for_function("!document.getElementById('char_202')")
            empty_remove_state = page.evaluate("""() => {
                const root = document.querySelector('[x-data^="productEditApp"]');
                return {
                    has_changes: Alpine.$data(root).hasChanges,
                    picker_focused: document.activeElement?.id === 'wb-optional-characteristic-picker',
                };
            }""")
            assert empty_remove_state == {"has_changes": False, "picker_focused": True}, empty_remove_state
            empty_attempt_after_remove = {
                "post_count": len(single_edit_post_requests),
                "fake_client_instances": REPORT["fake_wb_client_instances"],
                "fake_single_write_calls": REPORT["fake_wb_single_write_calls"],
                "provider_attempts": REPORT["provider_attempts"],
            }
            assert empty_attempt_after_remove == empty_attempt_before, empty_attempt_after_remove

            page.set_viewport_size({"width": 1280, "height": 900})
            wait_for_layout_settle(page, width=1280, theme="light")
            capture_layout(page, "single_product_edit")
            country_picker_keyboard = add_optional_field_with_keyboard(202)
            assert page.locator("#char_202").evaluate("el => el.tagName === 'SELECT'")
            assert ["Россия", "Китай"] == page.locator("#char_202 option").evaluate_all(
                "items => items.map(option => option.value).filter(Boolean)"
            )
            country_control_after_add = {
                "tag": page.locator("#char_202").evaluate("el => el.tagName"),
                "selected_value_before_edit": page.locator("#char_202").input_value(),
            }
            assert country_control_after_add == {
                "tag": "SELECT",
                "selected_value_before_edit": "",
            }, country_control_after_add
            REPORT["single_edit_observations"]["progressive_ui"] = {
                "initial_view": initial_progressive_view,
                "stale_legacy_field_read_only": stale_read_only_observation,
                "empty_optional_add_keyboard": empty_add_keyboard,
                "empty_add_enter_attempt": {
                    **empty_attempt_after_enter,
                    "post_count_delta": (
                        empty_attempt_after_enter["post_count"] - empty_attempt_before["post_count"]
                    ),
                    "fake_client_delta": (
                        empty_attempt_after_enter["fake_client_instances"]
                        - empty_attempt_before["fake_client_instances"]
                    ),
                    "fake_write_delta": (
                        empty_attempt_after_enter["fake_single_write_calls"]
                        - empty_attempt_before["fake_single_write_calls"]
                    ),
                },
                "empty_add_request_submit_attempt": {
                    **empty_attempt_after_request_submit,
                    "post_count_delta": (
                        empty_attempt_after_request_submit["post_count"] - empty_attempt_before["post_count"]
                    ),
                    "fake_client_delta": (
                        empty_attempt_after_request_submit["fake_client_instances"]
                        - empty_attempt_before["fake_client_instances"]
                    ),
                    "fake_write_delta": (
                        empty_attempt_after_request_submit["fake_single_write_calls"]
                        - empty_attempt_before["fake_single_write_calls"]
                    ),
                },
                "empty_optional_remove_keyboard": {
                    **empty_remove_state,
                    "focus": remove_button_focus,
                    "post_count_delta": 0,
                    "fake_client_delta": 0,
                    "fake_write_delta": 0,
                },
                "country_picker_add_keyboard": country_picker_keyboard,
                "country_control_after_add": country_control_after_add,
            }
            REPORT["checks"].append({
                "name": "single_edit_uses_cached_country_weight_multi_schema_and_read_only_sku",
                "status": "passed",
                "subject_id": 5880,
                "schema_fields": 31,
                "initial_visible_field_ids": initial_progressive_view["visible_field_ids"],
                "required_missing_id": 500,
                "stale_read_only_id": 501,
                "stale_read_only_disclosure_keyboard": stale_read_only_observation["disclosure_opened_by_keyboard"],
                "stale_read_only_displayed": (
                    stale_read_only_observation["field_text_present"]
                    and stale_read_only_observation["saved_value_present"]
                ),
                "stale_read_only_control_count": stale_read_only_observation["form_control_count"],
                "optional_country_picker_label": initial_progressive_view["picker_country_label"],
                "picker_keyboard_selection": 202,
                "present_empty_id": 506,
                "present_empty_visible": initial_progressive_view["present_empty_visible"],
                "present_empty_excluded_from_picker": initial_progressive_view["present_empty_excluded_from_picker"],
                "empty_add_remove_dirty": False,
                "empty_add_no_post_or_provider": empty_attempt_after_enter["post_count"] == empty_attempt_before["post_count"]
                    and empty_attempt_after_enter["fake_client_instances"] == empty_attempt_before["fake_client_instances"]
                    and empty_attempt_after_enter["fake_single_write_calls"] == empty_attempt_before["fake_single_write_calls"],
                "fake_wb_write_calls": REPORT["fake_wb_write_calls"],
            })

            # Use the page's normal HTML submit and CSRF field. The fake client
            # is the only provider boundary; the route performs schema parsing,
            # subject verification, local persistence and history creation.
            page.locator("#char_202").select_option("Россия")
            country_change_state = page.evaluate("""() => {
                const root = document.querySelector('[x-data^="productEditApp"]');
                const form = root?.querySelector('form.space-y-6');
                const app = Alpine.$data(root);
                return {
                    country: root?.querySelector('#char_202')?.value || '',
                    grams: root?.querySelector('#char_303')?.value || '',
                    materials: Array.from(root?.querySelectorAll('#char_404 option:checked') || [])
                        .map(option => option.value),
                    textarea: root?.querySelector('#char_502')?.value || '',
                    has_changes: app.hasChanges,
                    save_disabled: !!form?.querySelector('button[type="submit"]')?.disabled,
                };
            }""")
            assert country_change_state == {
                "country": "Россия",
                "grams": "125",
                "materials": ["Пластик", "Металл"],
                "textarea": "Existing line one\nExisting line two",
                "has_changes": True,
                "save_disabled": False,
            }, country_change_state

            # Measure the enabled form controls under both supported themes.
            # Choosing an optional field only enables the Add control; it is
            # deliberately not added, so the real form payload remains the
            # country-only change exercised below.
            optional_picker = page.locator("#wb-optional-characteristic-picker")
            optional_values = optional_picker.locator("option").evaluate_all(
                "options => options.map(option => option.value).filter(Boolean)"
            )
            assert optional_values, "synthetic schema must expose an optional field for contrast measurement"
            contrast_picker_value = optional_values[0]
            optional_picker.select_option(contrast_picker_value)
            assert page.locator("#wb-add-optional-characteristic").is_enabled()
            page.set_viewport_size({"width": 390, "height": 900})
            page.locator('label[for="wb-optional-characteristic-picker"]').scroll_into_view_if_needed()
            for contrast_theme in ("light", "dark"):
                page.evaluate(
                    "theme => document.documentElement.setAttribute('data-theme', theme)",
                    contrast_theme,
                )
                wait_for_layout_settle(page, width=390, theme=contrast_theme)
                appearance_stability = wait_for_wb_editor_appearance_settle(page)
                contrast_row = capture_wb_editor_control_contrast(page, theme=contrast_theme)
                contrast_row["picker_option_value"] = contrast_picker_value
                contrast_row["appearance_stability"] = appearance_stability
                contrast_row["measurement_valid"] = appearance_stability["settled"]
                if not appearance_stability["settled"]:
                    for control in contrast_row["controls"]:
                        control["contrast_ratio_estimate"] = None
                        control["normal_text_wcag_aa"] = None
                REPORT["single_edit_contrast_diagnostic"].append(contrast_row)
            optional_picker.select_option("")
            page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
            page.set_viewport_size({"width": 1280, "height": 900})
            wait_for_layout_settle(page, width=1280, theme="light")
            post_contrast_state = page.evaluate("""() => {
                const root = document.querySelector('[x-data^="productEditApp"]');
                const app = Alpine.$data(root);
                return {
                    country: root?.querySelector('#char_202')?.value || '',
                    selected_optional_id: root?.querySelector('#wb-optional-characteristic-picker')?.value || '',
                    has_changes: app.hasChanges,
                    save_disabled: !!root?.querySelector('form.space-y-6 button[type="submit"]')?.disabled,
                };
            }""")
            assert post_contrast_state == {
                "country": "Россия",
                "selected_optional_id": "",
                "has_changes": True,
                "save_disabled": False,
            }, post_contrast_state
            expected_contrast_controls = [
                "cancel",
                "optional_picker_label",
                "optional_picker",
                "optional_add",
                "save",
            ]
            contrast_rows = REPORT["single_edit_contrast_diagnostic"]
            theme_rows_valid = (
                len(contrast_rows) == 2
                and [row.get("requested_theme") for row in contrast_rows] == ["light", "dark"]
                and [row.get("actual_theme") for row in contrast_rows] == ["light", "dark"]
            )
            contrast_failures = []
            for row in contrast_rows:
                stability = row.get("appearance_stability") or {}
                controls = row.get("controls") or []
                if (
                    row.get("measurement_valid") is not True
                    or stability.get("settled") is not True
                    or stability.get("stable_frames", 0) < 3
                    or stability.get("active_relevant_transitions") != []
                    or row.get("viewport", {}).get("width") != 390
                ):
                    contrast_failures.append({"theme": row.get("requested_theme"), "reason": "appearance_not_settled"})
                if [control.get("name") for control in controls] != expected_contrast_controls:
                    contrast_failures.append({"theme": row.get("requested_theme"), "reason": "control_set_mismatch"})
                    continue
                for control in controls:
                    ratio = control.get("contrast_ratio_estimate")
                    if (
                        not control.get("visible")
                        or not control.get("enabled")
                        or control.get("disabled")
                        or not isinstance(control.get("text"), str)
                        or not control["text"].strip()
                        or not isinstance(ratio, (int, float))
                        or isinstance(ratio, bool)
                        or ratio < 4.5
                        or control.get("normal_text_wcag_aa") is not True
                    ):
                        contrast_failures.append({
                            "theme": row.get("requested_theme"),
                            "control": control.get("name"),
                            "ratio": ratio,
                        })
            contrast_aa_passed = theme_rows_valid and not contrast_failures
            REPORT["checks"].append({
                "name": "single_edit_enabled_controls_contrast_aa_light_dark",
                "status": "passed" if contrast_aa_passed else "failed",
                "themes": ["light", "dark"],
                "control_names": expected_contrast_controls,
                "control_count": len(contrast_rows) * len(expected_contrast_controls),
                "minimum_contrast_ratio": 4.5,
                "all_enabled_visible": contrast_aa_passed,
                "all_settled": all(
                    (row.get("appearance_stability") or {}).get("settled") is True
                    and row.get("measurement_valid") is True
                    for row in contrast_rows
                ),
                "all_contrast_aa": not contrast_failures,
                "failures": contrast_failures,
            })
            assert contrast_aa_passed, contrast_failures

            single_form = page.locator("form.space-y-6")
            assert single_form.locator('input[name="csrf_token"]').count() == 1
            assert single_form.locator('input[name="schema_revision"]').input_value()
            single_submit_button = single_form.get_by_role(
                "button", name="Сохранить в WB", exact=True,
            )
            submit_button_label = single_submit_button.inner_text().strip()
            required_missing_value = page.locator("#char_500").input_value()
            assert submit_button_label == "Сохранить в WB", submit_button_label
            assert required_missing_value == "", required_missing_value
            with seller_app.app_context():
                persisted_product = Product.query.filter_by(
                    id=fixture["product_id"], seller_id=fixture["seller_id"],
                ).one()
                persisted_core_values = {
                    "vendor_code": persisted_product.vendor_code,
                    "title": persisted_product.title,
                    "description": persisted_product.description,
                    "brand": persisted_product.brand,
                }
            expected_form_values = {
                field: value if isinstance(value, str) else ""
                for field, value in persisted_core_values.items()
            }
            core_form_locators = {
                "vendor_code": single_form.locator("#vendor_code"),
                "title": single_form.locator("#title"),
                "description": single_form.locator("#description"),
                "brand": single_form.locator("#brand"),
            }
            initial_core_form_values = {
                field: locator.input_value()
                for field, locator in core_form_locators.items()
            }
            initial_core_mismatches = sorted(
                field for field, value in initial_core_form_values.items()
                if value != expected_form_values[field]
            )
            assert persisted_core_values == {
                "vendor_code": "PD-000",
                "title": "  Pipedream Synthetic Product 000  ",
                "description": None,
                "brand": None,
            }, persisted_core_values
            assert initial_core_form_values == expected_form_values, {
                "expected": expected_form_values,
                "actual": initial_core_form_values,
            }
            assert initial_core_mismatches == [], initial_core_mismatches
            REPORT["single_edit_observations"]["core_form_alignment"] = {
                "persisted_core_values": persisted_core_values,
                "expected_form_values": expected_form_values,
                "initial_form_values": initial_core_form_values,
                "initial_mismatch_fields": initial_core_mismatches,
                "pre_submit_form_values": None,
                "pre_submit_changed_fields": None,
                "exact_before_characteristic_submit": True,
            }
            footer_clearance_check = capture_single_edit_footer_clearance(
                page,
                single_edit_post_requests=single_edit_post_requests,
                browser_mutation_requests=browser_mutation_requests,
                product_edit_path=f"/products/{fixture['product_id']}/edit",
            )
            REPORT["checks"].append(footer_clearance_check)
            assert footer_clearance_check["status"] == "passed", footer_clearance_check
            pre_submit_core_form_values = {
                field: locator.input_value()
                for field, locator in core_form_locators.items()
            }
            pre_submit_core_changes = sorted(
                field for field, value in pre_submit_core_form_values.items()
                if value != initial_core_form_values[field]
            )
            assert pre_submit_core_form_values == initial_core_form_values, {
                "initial": initial_core_form_values,
                "before_submit": pre_submit_core_form_values,
            }
            assert pre_submit_core_changes == [], pre_submit_core_changes
            REPORT["single_edit_observations"]["core_form_alignment"].update({
                "pre_submit_form_values": pre_submit_core_form_values,
                "pre_submit_changed_fields": pre_submit_core_changes,
            })
            check(
                "single_edit_form_core_fields_match_persisted_values_before_targeted_characteristic_save",
                core_fields=sorted(persisted_core_values),
                persisted_null_fields=sorted(
                    field for field, value in persisted_core_values.items()
                    if value is None
                ),
                legacy_whitespace_fields=["title"],
                initial_form_matches_persisted=True,
                pre_submit_core_fields_untouched=True,
                initial_mismatch_fields=initial_core_mismatches,
                pre_submit_changed_fields=pre_submit_core_changes,
            )
            single_path = f"/products/{fixture['product_id']}/edit"
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + single_path,
                timeout=5000,
            ) as single_post:
                with page.expect_navigation(wait_until="domcontentloaded"):
                    single_submit_button.click()
            assert single_post.value.status == 302
            assert urlsplit(page.url).path == f"/products/{fixture['product_id']}"
            assert "Карточка успешно обновлена на Wildberries" in page.locator("body").inner_text()
            assert REPORT["fake_wb_single_write_calls"] == 1
            assert REPORT["fake_wb_write_calls"] == 1
            single_fake_write = REPORT["fake_wb_single_write_requests"][0]
            assert REPORT["fake_wb_single_write_requests"] == [{
                "nm_id": 900000,
                "requested_fields": ["characteristics"],
                "core_fields_requested": [],
                "core_fields_changed": [],
                "characteristic_ids": [202],
                "characteristics": [
                    {"id": 202, "value": ["Россия"]},
                ],
                "fake_provider_core_before": {
                    "vendor_code": "PD-000",
                    "title": "  Pipedream Synthetic Product 000  ",
                    "description": None,
                    "brand": None,
                },
                "fake_provider_core_after": {
                    "vendor_code": "PD-000",
                    "title": "  Pipedream Synthetic Product 000  ",
                    "description": None,
                    "brand": None,
                },
                "full_card_read_before": True,
                "full_card_patch_merged": True,
                "full_card_readback": True,
                "sizes_preserved_in_readback": True,
                "sku_preserved_in_readback": True,
            }]
            route_history_fields = [
                field_name
                for provider_field, field_name in (
                    ("vendorCode", "vendor_code"),
                    ("title", "title"),
                    ("description", "description"),
                    ("brand", "brand"),
                    ("characteristics", "characteristics"),
                )
                if provider_field in single_fake_write["requested_fields"]
            ]
            saved_single_state = assert_single_edit_local_state(
                seller_app, fixture, expected_history_count=1,
                expected_changed_fields=route_history_fields,
            )
            REPORT["single_edit_observations"]["form_post"] = {
                "http_status": single_post.value.status,
                "path": single_path,
                "submit_button_label": submit_button_label,
                "target_channel": "Wildberries",
                "required_missing_id": 500,
                "required_missing_value": required_missing_value,
                "required_missing_omitted_from_patch": 500 not in single_fake_write["characteristic_ids"],
                "normal_html_form": True,
                "csrf_field_present": True,
                "fake_write_count": REPORT["fake_wb_single_write_calls"],
                "readback_and_history": saved_single_state,
            }
            check(
                "single_edit_real_form_submit_reaches_fake_wb_and_persists_exact_history",
                fake_wb_single_write_calls=REPORT["fake_wb_single_write_calls"],
                requested_fields=single_fake_write["requested_fields"],
                changed_characteristics=[202],
                submit_button_label=submit_button_label,
                target_channel="Wildberries",
                required_missing_value=required_missing_value,
                required_missing_omitted_from_patch=(
                    500 not in single_fake_write["characteristic_ids"]
                ),
                direct_history_count=saved_single_state["direct_history_count"],
                sizes_and_sku_preserved=True,
            )
            check(
                "single_edit_fake_provider_full_read_merge_readback_preserves_core_fields_sizes_and_sku",
                full_card_read_before=single_fake_write["full_card_read_before"],
                full_card_patch_merged=single_fake_write["full_card_patch_merged"],
                full_card_readback=single_fake_write["full_card_readback"],
                requested_fields=single_fake_write["requested_fields"],
                core_fields_changed=single_fake_write["core_fields_changed"],
                sizes_preserved=single_fake_write["sizes_preserved_in_readback"],
                sku_preserved=single_fake_write["sku_preserved_in_readback"],
                history_changed_fields=saved_single_state["history_changed_fields"],
            )
            interaction("single_characteristic_edit_uses_real_csrf_form_and_fake_provider_boundary")

            page.goto(BASE + f"/products/{fixture['product_id']}/edit", wait_until="domcontentloaded")
            reopened_core_form_values = {
                "vendor_code": page.locator("#vendor_code").input_value(),
                "title": page.locator("#title").input_value(),
                "description": page.locator("#description").input_value(),
                "brand": page.locator("#brand").input_value(),
            }
            assert reopened_core_form_values == expected_form_values, {
                "expected": expected_form_values,
                "actual": reopened_core_form_values,
            }
            assert page.locator("#char_202").input_value() == "Россия"
            assert page.locator("#char_303").input_value() == "125"
            assert page.locator("#char_404 option:checked").evaluate_all(
                "options => options.map(option => option.value)"
            ) == ["Пластик", "Металл"]
            assert page.locator("#char_506").input_value() == ""
            assert "Сохранено" in page.locator('label[for="char_506"]').inner_text()
            assert "SYNTHETIC-WB-SKU-000" in page.locator("body").inner_text()
            assert page.locator('input[name="sku"], input[name="sizes_json"]').count() == 0
            REPORT["single_edit_observations"]["reopen"] = {
                "core_form_values": reopened_core_form_values,
                "persisted_null_fields": ["brand", "description"],
                "legacy_whitespace_title_preserved": (
                    reopened_core_form_values["title"]
                    == "  Pipedream Synthetic Product 000  "
                ),
                "country": page.locator("#char_202").input_value(),
                "weight_grams": int(page.locator("#char_303").input_value()),
                "materials": page.locator("#char_404 option:checked").evaluate_all(
                    "options => options.map(option => option.value)"
                ),
                "present_empty_field_preserved": page.locator("#char_506").input_value() == ""
                    and "Сохранено" in page.locator('label[for="char_506"]').inner_text(),
                "sku_read_only": True,
            }
            check(
                "single_edit_reopens_exact_saved_values_with_sizes_and_sku_read_only",
                core_form_values=reopened_core_form_values,
                persisted_null_fields=["brand", "description"],
                legacy_whitespace_title_preserved=True,
                reopened_country="Россия",
                reopened_weight_grams=125,
                reopened_materials=["Пластик", "Металл"],
                present_empty_field_preserved=True,
                readback=saved_single_state,
            )
            interaction("single_edit_reopened_from_persisted_local_readback")

            # Use the actual form submission for malformed field types/units
            # and a value that is absent from the cached dictionary. The
            # hidden duplicate is inserted before its visible control to model
            # a forged request while keeping normal browser POST + CSRF.
            boundary_cases = [
                ("wrong_weight_unit", 303, "125 кг", "требуется число"),
                ("non_numeric_weight_type", 303, "not-a-number", "требуется число"),
                ("unlisted_dictionary_value", 404, "Хлопок", "отсутствует в словаре WB"),
            ]
            single_client_count_before_boundaries = REPORT["fake_wb_client_instances"]
            for boundary_name, char_id, bad_value, expected_error in boundary_cases:
                page.goto(BASE + f"/products/{fixture['product_id']}/edit", wait_until="domcontentloaded")
                if char_id == 303:
                    page.locator("#char_303").evaluate("""(control, value) => {
                        const form = control.form;
                        form.noValidate = true;
                        const injected = document.createElement('input');
                        injected.type = 'hidden';
                        injected.name = control.name;
                        injected.value = value;
                        control.before(injected);
                        control.dispatchEvent(new Event('input', {bubbles: true}));
                    }""", bad_value)
                else:
                    page.locator("#char_404").evaluate("""(control, value) => {
                        const form = control.form;
                        form.noValidate = true;
                        for (const option of control.options) option.selected = false;
                        const injected = document.createElement('input');
                        injected.type = 'hidden';
                        injected.name = control.name;
                        injected.value = value;
                        control.before(injected);
                        control.dispatchEvent(new Event('input', {bubbles: true}));
                    }""", bad_value)
                with page.expect_navigation(wait_until="domcontentloaded") as rejected_post:
                    page.locator('form.space-y-6 button[type="submit"]').click()
                assert rejected_post.value.status == 200
                assert urlsplit(page.url).path == f"/products/{fixture['product_id']}/edit"
                assert expected_error in page.locator("body").inner_text()
                state_after_rejection = assert_single_edit_local_state(
                    seller_app, fixture, expected_history_count=1,
                    expected_changed_fields=route_history_fields,
                )
                assert REPORT["fake_wb_single_write_calls"] == 1
                assert REPORT["fake_wb_write_calls"] == 1
                assert REPORT["fake_wb_client_instances"] == single_client_count_before_boundaries
                REPORT["single_edit_boundary_attempts"].append({
                    "name": boundary_name,
                    "status": rejected_post.value.status,
                    "provider_writes": REPORT["fake_wb_single_write_calls"],
                    "fake_client_instances": REPORT["fake_wb_client_instances"],
                    "history_count": state_after_rejection["direct_history_count"],
                    "local_product_preserved": True,
                })
                REPORT["single_edit_observations"]["rejections"].append({
                    "name": boundary_name,
                    "http_status": rejected_post.value.status,
                    "provider_writes": REPORT["fake_wb_single_write_calls"],
                    "local_product_preserved": True,
                    "history_count": state_after_rejection["direct_history_count"],
                })
                check(
                    f"single_edit_rejects_{boundary_name}_without_local_loss",
                    http_status=rejected_post.value.status,
                    provider_writes=REPORT["fake_wb_single_write_calls"],
                    fake_client_instances=REPORT["fake_wb_client_instances"],
                    history_count=state_after_rejection["direct_history_count"],
                    local_product_preserved=True,
                )
                interaction(f"invalid_single_edit_{boundary_name}_denied_before_fake_provider")

            unmapped_url = BASE + f"/products/{fixture['unmapped_product_id']}/edit"
            page.goto(unmapped_url, wait_until="domcontentloaded")
            assert_keyboard_focus(page, "unmapped_product_edit")
            unmapped_panel = page.locator("section[aria-labelledby='wb-characteristics-title']")
            assert "Категория Wildberries не подтверждена" in unmapped_panel.inner_text()
            assert "Характеристики недоступны без актуального локального справочника Wildberries." in unmapped_panel.inner_text()
            assert page.locator('input[name^="char_"], select[name^="char_"], textarea[name^="char_"]').count() == 0
            unmapped_panel.locator("summary").click()
            assert "Stale prior-subject field" in unmapped_panel.inner_text()
            assert "Preserve read-only historical value" in unmapped_panel.inner_text()
            capture_layout(page, "unmapped_product_edit")

            prior_fake_client_count = REPORT["fake_wb_client_instances"]
            stale_post = page.evaluate("""async ({path, value}) => {
                const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const body = new URLSearchParams({csrf_token: csrf, char_101: value}).toString();
                const response = await fetch(path, {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrf},
                    body,
                });
                return {status: response.status, html: await response.text()};
            }""", {
                "path": f"/products/{fixture['unmapped_product_id']}/edit",
                "value": "",
            })
            assert stale_post["status"] == 200
            assert "Точная локальная WB-схема устарела или недоступна" in stale_post["html"]
            assert REPORT["fake_wb_client_instances"] == prior_fake_client_count
            check(
                "unmapped_subject_keeps_old_characteristics_read_only_and_rejects_clear_post",
                status=stale_post["status"],
                fake_wb_client_instances=REPORT["fake_wb_client_instances"],
            )

            # Both directions of the seller boundary use a distinct product
            # owner. First the authenticated owner tries the foreign product;
            # then a clean foreign-owner session tries the authenticated
            # seller's product. A profile-less account is tested separately.
            provider_state_before_denials = (
                REPORT["fake_wb_client_instances"],
                REPORT["fake_wb_write_calls"],
                REPORT["fake_wb_single_write_calls"],
            )
            owner_to_foreign = post_from_isolated_session(
                page,
                path=f"/products/{fixture['foreign_product_id']}/edit",
            )
            assert owner_to_foreign["status"] == 404
            assert owner_to_foreign["path"] == f"/products/{fixture['foreign_product_id']}/edit"
            assert (
                REPORT["fake_wb_client_instances"],
                REPORT["fake_wb_write_calls"],
                REPORT["fake_wb_single_write_calls"],
            ) == provider_state_before_denials
            check(
                "single_edit_owner_session_cannot_post_foreign_product",
                http_status=owner_to_foreign["status"],
                provider_writes=REPORT["fake_wb_single_write_calls"],
            )
            REPORT["single_edit_observations"]["seller_scope_denials"].append({
                "session": "owner",
                "target": "foreign_product",
                "http_status": owner_to_foreign["status"],
                "fake_writes_unchanged": True,
            })

            foreign_context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            foreign_context.route(
                "**/*",
                lambda route: bridge(
                    route,
                    assets=assets,
                    origin=origin,
                    allowed_local_posts=allowed_local_posts,
                ),
            )
            foreign_page = foreign_context.new_page()
            foreign_page.set_default_timeout(15000)
            foreign_page.on("response", record_http_response)
            foreign_login = authenticate_isolated_browser_session(
                foreign_page,
                username="ux01-wb-edit-foreign",
                next_path="/products",
            )
            assert foreign_login["status"] == 200 and foreign_login["redirected"] is True
            foreign_to_owner = post_from_isolated_session(
                foreign_page,
                path=f"/products/{fixture['product_id']}/edit",
            )
            assert foreign_to_owner["status"] == 404
            assert foreign_to_owner["path"] == f"/products/{fixture['product_id']}/edit"
            assert (
                REPORT["fake_wb_client_instances"],
                REPORT["fake_wb_write_calls"],
                REPORT["fake_wb_single_write_calls"],
            ) == provider_state_before_denials
            check(
                "single_edit_foreign_owner_session_cannot_post_seller_product",
                http_status=foreign_to_owner["status"],
                separate_browser_session=True,
                provider_writes=REPORT["fake_wb_single_write_calls"],
            )
            REPORT["single_edit_observations"]["seller_scope_denials"].append({
                "session": "foreign_owner",
                "target": "seller_product",
                "http_status": foreign_to_owner["status"],
                "separate_browser_session": True,
                "fake_writes_unchanged": True,
            })
            foreign_context.close()

            no_profile_context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            no_profile_context.route(
                "**/*",
                lambda route: bridge(
                    route,
                    assets=assets,
                    origin=origin,
                    allowed_local_posts=allowed_local_posts,
                ),
            )
            no_profile_page = no_profile_context.new_page()
            no_profile_page.set_default_timeout(15000)
            no_profile_page.on("response", record_http_response)
            no_profile_login = authenticate_isolated_browser_session(
                no_profile_page,
                username=fixture["no_profile_username"],
                next_path="/dashboard",
            )
            assert no_profile_login["status"] == 200 and no_profile_login["redirected"] is True
            no_profile_attempt = post_from_isolated_session(
                no_profile_page,
                path=f"/products/{fixture['product_id']}/edit",
            )
            assert no_profile_attempt["status"] == 200
            assert no_profile_attempt["redirected"] is True
            assert no_profile_attempt["path"] == "/dashboard"
            assert (
                REPORT["fake_wb_client_instances"],
                REPORT["fake_wb_write_calls"],
                REPORT["fake_wb_single_write_calls"],
            ) == provider_state_before_denials
            check(
                "single_edit_no_profile_post_redirects_without_provider_write",
                final_path=no_profile_attempt["path"],
                separate_browser_session=True,
                provider_writes=REPORT["fake_wb_single_write_calls"],
            )
            REPORT["single_edit_observations"]["no_profile_denial"] = {
                "final_path": no_profile_attempt["path"],
                "redirected": no_profile_attempt["redirected"],
                "separate_browser_session": True,
                "fake_writes_unchanged": True,
            }
            no_profile_context.close()
            interaction("isolated_foreign_and_no_profile_sessions_are_denied_before_provider")

            # Restore the synthetic core values changed for the nullable-value
            # scenario so the later, separate 50-row brand filter still sees
            # the same fixture set it did before the single-edit checks.
            with seller_app.app_context():
                single_product = Product.query.filter_by(
                    id=fixture["product_id"], seller_id=fixture["seller_id"],
                ).one()
                for field, value in pre_single_core_values.items():
                    setattr(single_product, field, value)
                provider_card = FakeWBClient._read_full_card(900000)
                for field, value in pre_single_fake_core_values.items():
                    provider_card[field] = value
                _FAKE_WB_CARDS[900000] = FakeWBClient._copy_card(provider_card)
                db.session.commit()

            # Filter change resets the stored exact set rather than expanding
            # it silently. Then all-filtered is a separate explicit action;
            # unchecking one row records an exact exclusion.
            page.goto(BASE + "/products?search=Pipedream&brand=Synthetic%20reviewed%20brand&sort=title&order=asc&page=1&per_page=50")
            assert page.locator("#selectedCount").inner_text().strip() == "0"
            interaction("changing_filter_clears_prior_selection_without_expansion")
            select_filtered = page.get_by_role(
                "button", name="Выбрать все отфильтрованные (50)",
            )
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + "/products/selection/resolve",
                timeout=5000,
            ) as all_filtered_response:
                select_filtered.click()
            resolved_response = all_filtered_response.value
            assert resolved_response.status == 200
            resolver_request = json.loads(resolved_response.request.post_data or "{}")
            assert resolver_request.get("mode") == "all_filtered"
            assert resolver_request.get("ids") == []
            assert resolver_request.get("filters") == {
                "search": "Pipedream",
                "brand": "Synthetic reviewed brand",
                "active_only": False,
                "disabled_only": False,
                "category": "",
                "has_stock": "",
                "block_status": "",
                "rating_min": None,
                "rating_max": None,
                "quality_weak": False,
                "supplier_id": None,
            }
            assert resolver_request.get("sort") == "title"
            assert resolver_request.get("order") == "asc"
            assert resolver_request.get("page") == 1
            assert resolver_request.get("per_page") == 50
            resolver_ids = resolved_response.json().get("ids")
            assert isinstance(resolver_ids, list)
            assert sorted(int(product_id) for product_id in resolver_ids) == expected_product_ids
            page.wait_for_function(
                "expected => document.querySelector('#selectedCount')?.textContent?.trim() === String(expected)",
                arg=len(expected_product_ids),
                timeout=5000,
            )
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            selected_ids = page.locator(
                '#bulkActionForm input[name="product_ids"]',
            ).evaluate_all("inputs => inputs.map(input => Number(input.value))")
            assert sorted(selected_ids) == expected_product_ids
            check(
                "all_filtered_resolver_returns_exact_current_filter_ids",
                resolver_status=resolved_response.status,
                selected=len(selected_ids),
            )
            excluded_id = int(page.locator(".product-checkbox").first.get_attribute("value"))
            page.locator(".product-checkbox").first.uncheck()
            page.wait_for_function(
                "expected => document.querySelector('#selectedCount')?.textContent?.trim() === String(expected)",
                arg=len(expected_product_ids) - 1,
                timeout=5000,
            )
            assert page.locator("#selectedCount").inner_text().strip() == "49"
            selected_ids = page.locator(
                '#bulkActionForm input[name="product_ids"]',
            ).evaluate_all("inputs => inputs.map(input => Number(input.value))")
            assert sorted(selected_ids) == [
                product_id for product_id in expected_product_ids
                if product_id != excluded_id
            ]
            interaction("all_filtered_is_explicit_and_manual_uncheck_is_an_exact_exclusion")
            page.locator("button").filter(has_text="Отменить выбор").click()
            page.wait_for_function(
                "() => document.querySelector('#selectedCount')?.textContent?.trim() === '0'",
                timeout=5000,
            )
            assert page.locator("#selectedCount").inner_text().strip() == "0"

            # A second seller owns a separate exact 50-card fixture. Only two
            # rows have a positive nmID, so the real preview must report
            # selected=50, eligible=2, changed=2, skipped=48 and only those two
            # cards may cross the fake WB boundary.
            mixed_context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            mixed_context.route(
                "**/*",
                lambda route: bridge(
                    route,
                    assets=assets,
                    origin=origin,
                    allowed_local_posts=allowed_local_posts,
                ),
            )
            mixed_page = mixed_context.new_page()
            mixed_page.set_default_timeout(15000)
            mixed_page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
            mixed_page.on("response", record_http_response)
            mixed_login = authenticate_isolated_browser_session(
                mixed_page,
                username=fixture["mixed_username"],
                next_path="/products",
            )
            assert mixed_login["status"] == 200 and mixed_login["redirected"] is True
            mixed_query = {
                "search": ["Mixed"],
                "brand": ["Mixed Initial Brand"],
                "sort": ["title"],
                "order": ["asc"],
                "page": ["1"],
                "per_page": ["50"],
            }
            mixed_list_url = (
                BASE + "/products?search=Mixed&brand=Mixed%20Initial%20Brand"
                "&sort=title&order=asc&page=1&per_page=50"
            )
            mixed_page.goto(mixed_list_url, wait_until="domcontentloaded")
            _assert_local_products_url(mixed_page.url, mixed_query)
            assert mixed_page.locator(".product-checkbox").count() == 50
            mixed_list_ids = sorted(mixed_page.locator(
                ".product-checkbox",
            ).evaluate_all("inputs => inputs.map(input => Number(input.value))"))
            assert mixed_list_ids == fixture["mixed_product_ids"]

            with mixed_page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + "/products/selection/resolve",
                timeout=5000,
            ) as mixed_resolver:
                mixed_page.get_by_role(
                    "button", name="Выбрать все отфильтрованные (50)", exact=True,
                ).click()
            assert mixed_resolver.value.status == 200
            assert sorted(mixed_resolver.value.json()["ids"]) == fixture["mixed_product_ids"]
            mixed_page.wait_for_function(
                "() => document.querySelector('#selectedCount')?.textContent?.trim() === '50'",
                timeout=5000,
            )
            mixed_page.get_by_role("button", name="Редактировать", exact=True).click()
            mixed_page.wait_for_url("**/products/bulk-edit")
            assert_bulk_selected_count(mixed_page, 50)
            mixed_page.locator('input[name="operation"][value="update_brand"]').check()
            mixed_page.locator("#value_brand").fill("Mixed Reviewed Brand")
            mixed_fake_baseline = {
                "instances": REPORT["fake_wb_client_instances"],
                "write_calls": REPORT["fake_wb_write_calls"],
                "written_products": len(REPORT["fake_wb_written_products"]),
            }
            with mixed_page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + "/products/bulk-edit",
                timeout=5000,
            ) as mixed_preview_post:
                mixed_page.locator(
                    'form[action="/products/bulk-edit"] button[type="submit"]',
                ).click()
            assert mixed_preview_post.value.status == 200
            mixed_page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(mixed_page, changed="2", skipped="48")
            mixed_counts = mixed_page.locator(
                "section[aria-label='Сводка предпросмотра'] div.sh-card",
            ).evaluate_all("""cards => Object.fromEntries(cards.map(card => [
                card.querySelector('p.text-xs').textContent.trim(),
                Number(card.querySelector('p.text-2xl').textContent.trim()),
            ]))""")
            assert mixed_counts == {
                "Выбрано": 50,
                "Подходит для операции": 2,
                "Изменится": 2,
                "Пропущено": 48,
                "Ошибки": 0,
            }, mixed_counts
            assert mixed_page.locator(
                'section.sh-card--flush tbody tr',
            ).count() == 2
            assert REPORT["fake_wb_client_instances"] == mixed_fake_baseline["instances"]
            assert REPORT["fake_wb_write_calls"] == mixed_fake_baseline["write_calls"]
            assert len(REPORT["fake_wb_written_products"]) == mixed_fake_baseline["written_products"]
            assert mixed_fake_baseline["write_calls"] == 1
            check(
                "mixed_fixture_preview_selected50_eligible2_changed2_skipped48",
                counts=mixed_counts,
                diff_rows=2,
                fake_provider_writes_before_confirm=0,
            )
            interaction("isolated_mixed_fixture_review_keeps_48_unlinked_products_skipped")

            with mixed_page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + "/products/bulk-edit",
                timeout=5000,
            ) as mixed_apply_post:
                with mixed_page.expect_navigation(wait_until="domcontentloaded"):
                    mixed_page.get_by_role(
                        "button", name="Подтвердить и применить 2 карточки", exact=True,
                    ).click()
            assert mixed_apply_post.value.status == 302
            assert "Успешно обновлено товаров: 2" in mixed_page.locator("body").inner_text()
            mixed_written = REPORT["fake_wb_written_products"][
                mixed_fake_baseline["written_products"]:
            ]
            assert sorted(mixed_written) == [910000, 910001], mixed_written
            assert REPORT["fake_wb_write_calls"] - mixed_fake_baseline["write_calls"] == 1
            assert REPORT["fake_wb_client_instances"] - mixed_fake_baseline["instances"] == 1
            assert REPORT["provider_attempts"] == 0

            from models import CardEditHistory, BulkEditHistory, Product
            with seller_app.app_context():
                mixed_history = BulkEditHistory.query.filter_by(
                    seller_id=fixture["mixed_seller_id"],
                    operation_type="update_brand",
                ).one()
                assert mixed_history.status == "completed"
                assert mixed_history.total_products == 50
                assert mixed_history.success_count == 2
                assert mixed_history.error_count == 0
                mixed_summary = (mixed_history.operation_params or {}).get("review_summary") or {}
                assert mixed_summary == {
                    "selected": 50,
                    "eligible": 2,
                    "changed": 2,
                    "skipped": 48,
                    "errors": 0,
                    "changed_product_ids": fixture["mixed_changed_product_ids"],
                    "mode": "replace",
                    "subject_id": None,
                }, mixed_summary
                mixed_history_rows = CardEditHistory.query.filter_by(
                    seller_id=fixture["mixed_seller_id"],
                    bulk_edit_id=mixed_history.id,
                ).order_by(CardEditHistory.product_id.asc()).all()
                assert [row.product_id for row in mixed_history_rows] == fixture[
                    "mixed_changed_product_ids"
                ]
                assert all(
                    row.changed_fields == ["brand"]
                    and row.wb_synced is True
                    and row.wb_sync_status == "success"
                    and row.snapshot_before["brand"] == "Mixed Initial Brand"
                    and row.snapshot_after["brand"] == "Mixed Reviewed Brand"
                    for row in mixed_history_rows
                )
                mixed_products = Product.query.filter(
                    Product.id.in_(fixture["mixed_product_ids"]),
                ).order_by(Product.id.asc()).all()
                assert [product.brand for product in mixed_products] == [
                    "Mixed Reviewed Brand", "Mixed Reviewed Brand",
                    *(["Mixed Initial Brand"] * 48),
                ]

            mixed_history_id = int(mixed_history.id)
            mixed_page.goto(
                BASE + f"/bulk-history/{mixed_history_id}",
                wait_until="domcontentloaded",
            )
            assert "Изменённые товары (2)" in mixed_page.locator("body").inner_text()
            mixed_history_product_ids = sorted(mixed_page.locator(
                "[data-operations-product-id]",
            ).evaluate_all(
                "rows => rows.map(row => Number(row.dataset.operationsProductId))",
            ))
            assert mixed_history_product_ids == fixture["mixed_changed_product_ids"]
            assert mixed_page.locator('[data-operations-changed-field="brand"]').count() == 2
            REPORT["mixed_fixture_observations"] = {
                "selection": 50,
                "eligible": 2,
                "changed": 2,
                "skipped": 48,
                "errors": 0,
                "fake_provider_call_delta": REPORT["fake_wb_write_calls"] - mixed_fake_baseline["write_calls"],
                "fake_provider_product_ids": sorted(mixed_written),
                "history_id": mixed_history_id,
                "history_product_ids": mixed_history_product_ids,
                "history_success_count": mixed_history.success_count,
            }
            check(
                "mixed_fixture_confirm_writes_exact_two_provider_products_with_history_readback",
                observation=REPORT["mixed_fixture_observations"],
            )
            interaction("mixed_fixture_confirm_writes_only_reviewed_rows_and_history_reads_back")

            # A separate exact two-row selection verifies local exclusion:
            # it must issue a fresh one-product preview before the ordinary
            # apply button can write, without changing the preceding 50-row
            # mixed-scope proof.
            excluded_product_id, remaining_product_id = fixture[
                "mixed_changed_product_ids"
            ]
            remaining_nm_id = fixture["mixed_nm_ids"][1]
            exclusion_flow_baseline = {
                "client_instances": REPORT["fake_wb_client_instances"],
                "write_calls": REPORT["fake_wb_write_calls"],
                "written_products": len(REPORT["fake_wb_written_products"]),
            }
            mixed_page.goto(
                BASE + "/products?search=Mixed&brand=Mixed%20Reviewed%20Brand"
                "&sort=title&order=asc&page=1&per_page=50",
                wait_until="domcontentloaded",
            )
            _assert_local_products_url(mixed_page.url, {
                "search": ["Mixed"],
                "brand": ["Mixed Reviewed Brand"],
                "sort": ["title"],
                "order": ["asc"],
                "page": ["1"],
                "per_page": ["50"],
            })
            assert mixed_page.locator(".product-checkbox").count() == 2
            filtered_ids = sorted(int(value) for value in mixed_page.locator(
                ".product-checkbox",
            ).evaluate_all("inputs => inputs.map(input => input.value)"))
            assert filtered_ids == fixture["mixed_changed_product_ids"], filtered_ids
            for product_id in filtered_ids:
                mixed_page.locator(
                    f'.product-checkbox[value="{product_id}"]',
                ).check()
            mixed_page.wait_for_function(
                "() => document.querySelector('#selectedCount')?.textContent?.trim() === '2'",
                timeout=5000,
            )
            mixed_page.get_by_role("button", name="Редактировать", exact=True).click()
            mixed_page.wait_for_url("**/products/bulk-edit")
            mixed_page.locator('input[name="operation"][value="update_brand"]').check()
            mixed_page.locator("#value_brand").fill("One Product After Exclusion")
            with mixed_page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + "/products/bulk-edit",
                timeout=5000,
            ) as exclusion_source_preview:
                with mixed_page.expect_navigation(wait_until="domcontentloaded"):
                    mixed_page.locator(
                        'form[action="/products/bulk-edit"] button[type="submit"]',
                    ).click()
            assert exclusion_source_preview.value.status == 200
            initial_preview_ids = sorted(int(value) for value in mixed_page.locator(
                "[data-review-product-id]",
            ).evaluate_all("nodes => nodes.map(node => node.dataset.reviewProductId)"))
            assert initial_preview_ids == filtered_ids, initial_preview_ids
            assert mixed_page.locator(
                '[data-review-product-id="' + str(excluded_product_id) + '"]',
            ).count() == 1
            assert mixed_page.locator(
                '[data-review-product-id="' + str(remaining_product_id) + '"]',
            ).count() == 1
            initial_exclusion_summary = mixed_page.locator(
                "section[aria-label='Сводка предпросмотра'] div.sh-card",
            ).evaluate_all("""cards => Object.fromEntries(cards.map(card => [
                card.querySelector('p.text-xs').textContent.trim(),
                Number(card.querySelector('p.text-2xl').textContent.trim()),
            ]))""")
            assert initial_exclusion_summary == {
                "Выбрано": 2,
                "Подходит для операции": 2,
                "Изменится": 2,
                "Пропущено": 0,
                "Ошибки": 0,
            }, initial_exclusion_summary

            exclusion_form = mixed_page.locator("form#wb-review-exclude")
            assert exclusion_form.count() == 1
            exclusion_form_names = exclusion_form.locator("input").evaluate_all(
                "inputs => inputs.map(input => input.name)",
            )
            assert exclusion_form_names == [
                "csrf_token", "selection_token", "review_exclude", "base_preview_token",
            ], exclusion_form_names
            assert exclusion_form.locator(
                'input[name="review_exclude"]',
            ).input_value() == "1"
            assert exclusion_form.locator(
                'input[name="base_preview_token"]',
            ).input_value()
            exclude_button = mixed_page.locator(
                f'[data-review-desktop-table] '
                f'button[data-review-exclude-product-id="{excluded_product_id}"]',
            )
            assert exclude_button.count() == 1
            exclude_name = exclude_button.get_attribute("aria-label") or ""
            assert "Mixed isolated product 000" in exclude_name, exclude_name
            assert f"ID Seller Hub {excluded_product_id}" in exclude_name, exclude_name
            assert exclude_button.evaluate(
                "button => button.form?.id === 'wb-review-exclude'"
                " && button.type === 'submit'"
                " && button.name === 'excluded_product_id'"
                " && Number(button.value) > 0",
            ) is True
            assert exclude_button.inner_text().strip() == "Исключить из проверки"
            assert REPORT["fake_wb_client_instances"] == mixed_fake_baseline["instances"] + 1
            assert REPORT["fake_wb_write_calls"] == mixed_fake_baseline["write_calls"] + 1
            assert REPORT["fake_wb_client_instances"] == exclusion_flow_baseline["client_instances"]
            assert REPORT["fake_wb_write_calls"] == exclusion_flow_baseline["write_calls"]

            with mixed_page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + "/products/bulk-edit",
                timeout=5000,
            ) as exclusion_response:
                with mixed_page.expect_navigation(wait_until="domcontentloaded"):
                    exclude_button.click()
            assert exclusion_response.value.status == 200
            exclusion_request_fields = parse_qs(
                exclusion_response.value.request.post_data or "",
                keep_blank_values=True,
            )
            assert set(exclusion_request_fields) == {
                "csrf_token", "selection_token", "review_exclude",
                "base_preview_token", "excluded_product_id",
            }, exclusion_request_fields.keys()
            assert exclusion_request_fields["review_exclude"] == ["1"]
            assert exclusion_request_fields["excluded_product_id"] == [
                str(excluded_product_id),
            ]
            assert exclusion_request_fields["base_preview_token"]
            assert exclusion_request_fields["selection_token"]
            assert mixed_page.locator(
                '[data-review-product-id="' + str(excluded_product_id) + '"]',
            ).count() == 0
            assert mixed_page.locator(
                '[data-review-product-id="' + str(remaining_product_id) + '"]',
            ).count() == 1
            assert mixed_page.locator("form#wb-review-exclude").count() == 0, (
                "A one-row preview must not offer an exclusion that would empty the selection"
            )
            reduced_preview_ids = sorted(int(value) for value in mixed_page.locator(
                "[data-review-product-id]",
            ).evaluate_all("nodes => nodes.map(node => node.dataset.reviewProductId)"))
            assert reduced_preview_ids == [remaining_product_id], reduced_preview_ids
            reduced_summary = mixed_page.locator(
                "section[aria-label='Сводка предпросмотра'] div.sh-card",
            ).evaluate_all("""cards => Object.fromEntries(cards.map(card => [
                card.querySelector('p.text-xs').textContent.trim(),
                Number(card.querySelector('p.text-2xl').textContent.trim()),
            ]))""")
            assert reduced_summary == {
                "Выбрано": 1,
                "Подходит для операции": 1,
                "Изменится": 1,
                "Пропущено": 0,
                "Ошибки": 0,
            }, reduced_summary
            assert mixed_page.locator(
                '[data-review-product-id-mobile="' + str(remaining_product_id) + '"]',
            ).count() == 1
            assert mixed_page.locator(
                '[data-review-product-change-mobile]',
            ).count() == 1
            assert REPORT["fake_wb_client_instances"] == mixed_fake_baseline["instances"] + 1
            assert REPORT["fake_wb_write_calls"] == mixed_fake_baseline["write_calls"] + 1
            assert REPORT["fake_wb_client_instances"] == exclusion_flow_baseline["client_instances"]
            assert REPORT["fake_wb_write_calls"] == exclusion_flow_baseline["write_calls"]
            exclusion_preview_provider_client_delta = (
                REPORT["fake_wb_client_instances"] - exclusion_flow_baseline["client_instances"]
            )
            exclusion_preview_provider_write_delta = (
                REPORT["fake_wb_write_calls"] - exclusion_flow_baseline["write_calls"]
            )
            check(
                "wb_review_exclusion_rebuilds_exact_smaller_preview_without_write",
                selected=1,
                changed=1,
                excluded_product_id=excluded_product_id,
                remaining_product_id=remaining_product_id,
                provider_calls_before_apply=1,
                provider_write_calls_before_apply=1,
            )

            with mixed_page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == BASE + "/products/bulk-edit",
                timeout=5000,
            ) as exclusion_apply_response:
                with mixed_page.expect_navigation(wait_until="domcontentloaded"):
                    mixed_page.get_by_role(
                        "button", name="Подтвердить и применить 1 карточка", exact=True,
                    ).click()
            assert exclusion_apply_response.value.status == 302
            assert "Успешно обновлено товаров: 1" in mixed_page.locator("body").inner_text()
            exclusion_writes = REPORT["fake_wb_written_products"][
                mixed_fake_baseline["written_products"] + 2:
            ]
            assert exclusion_writes == [remaining_nm_id], exclusion_writes
            assert REPORT["fake_wb_write_calls"] - mixed_fake_baseline["write_calls"] == 2
            assert REPORT["fake_wb_client_instances"] - mixed_fake_baseline["instances"] == 2
            assert REPORT["fake_wb_client_instances"] - exclusion_flow_baseline["client_instances"] == 1
            assert REPORT["fake_wb_write_calls"] - exclusion_flow_baseline["write_calls"] == 1
            with seller_app.app_context():
                exclusion_products = Product.query.filter(
                    Product.id.in_(fixture["mixed_changed_product_ids"]),
                ).order_by(Product.id.asc()).all()
                assert [product.brand for product in exclusion_products] == [
                    "Mixed Reviewed Brand", "One Product After Exclusion",
                ]
                exclusion_history = BulkEditHistory.query.filter_by(
                    seller_id=fixture["mixed_seller_id"],
                    operation_type="update_brand",
                ).order_by(BulkEditHistory.id.desc()).first()
                assert exclusion_history.status == "completed"
                assert exclusion_history.total_products == 1
                assert exclusion_history.success_count == 1
                assert exclusion_history.error_count == 0
                exclusion_summary = (
                    exclusion_history.operation_params or {}
                ).get("review_summary") or {}
                assert exclusion_summary == {
                    "selected": 1,
                    "eligible": 1,
                    "changed": 1,
                    "skipped": 0,
                    "errors": 0,
                    "changed_product_ids": [remaining_product_id],
                    "mode": "replace",
                    "subject_id": None,
                }, exclusion_summary
                exclusion_local_brands = [product.brand for product in exclusion_products]
                exclusion_history_observation = {
                    "status": exclusion_history.status,
                    "total_products": exclusion_history.total_products,
                    "success_count": exclusion_history.success_count,
                    "error_count": exclusion_history.error_count,
                    "changed_product_ids": exclusion_summary["changed_product_ids"],
                }
            assert exclusion_local_brands == [
                "Mixed Reviewed Brand", "One Product After Exclusion",
            ], exclusion_local_brands
            REPORT["review_exclusion_observations"] = {
                "initial_selected_product_ids": filtered_ids,
                "initial_preview_product_ids": initial_preview_ids,
                "initial_preview_summary": {
                    "selected": initial_exclusion_summary["Выбрано"],
                    "eligible": initial_exclusion_summary["Подходит для операции"],
                    "changed": initial_exclusion_summary["Изменится"],
                    "skipped": initial_exclusion_summary["Пропущено"],
                    "errors": initial_exclusion_summary["Ошибки"],
                },
                "excluded_product_id": excluded_product_id,
                "exclusion_post_status": exclusion_response.value.status,
                "exclusion_request_fields": sorted(exclusion_request_fields),
                "exclusion_request_product_id": int(
                    exclusion_request_fields["excluded_product_id"][0]
                ),
                "selection_token_present": bool(exclusion_request_fields["selection_token"]),
                "base_preview_token_present": bool(exclusion_request_fields["base_preview_token"]),
                "rebuilt_preview_product_ids": reduced_preview_ids,
                "rebuilt_preview_summary": {
                    "selected": reduced_summary["Выбрано"],
                    "eligible": reduced_summary["Подходит для операции"],
                    "changed": reduced_summary["Изменится"],
                    "skipped": reduced_summary["Пропущено"],
                    "errors": reduced_summary["Ошибки"],
                },
                "excluded_row_absent": mixed_page.locator(
                    '[data-review-product-id="' + str(excluded_product_id) + '"]',
                ).count() == 0,
                "exclude_control_removed_for_one_row": mixed_page.locator(
                    "form#wb-review-exclude",
                ).count() == 0,
                "exclusion_preview_provider_client_delta": exclusion_preview_provider_client_delta,
                "exclusion_preview_provider_write_delta": exclusion_preview_provider_write_delta,
                "apply_status": exclusion_apply_response.value.status,
                "applied_provider_nm_ids": exclusion_writes,
                "apply_provider_client_delta": (
                    REPORT["fake_wb_client_instances"] - exclusion_flow_baseline["client_instances"]
                ),
                "apply_provider_write_delta": (
                    REPORT["fake_wb_write_calls"] - exclusion_flow_baseline["write_calls"]
                ),
                "local_brand_values_after_apply": exclusion_local_brands,
                "history": exclusion_history_observation,
            }
            check(
                "wb_review_exclusion_apply_reaches_only_remaining_product",
                selected_product_id=remaining_product_id,
                excluded_product_id=excluded_product_id,
                provider_nm_id=remaining_nm_id,
                local_changes=1,
                observation=REPORT["review_exclusion_observations"],
            )
            interaction("mixed_review_excludes_exact_product_then_applies_remainder")
            mixed_context.close()

            assert REPORT["provider_attempts"] == 0
            assert REPORT["javascript_errors"] == []
            assert REPORT["unexpected_external_requests"] == []
            assert REPORT["unexpected_http"] == []
            missing_negative = set(EXPECTED_NEGATIVE_HTTP).difference(OBSERVED_EXPECTED_NEGATIVE_HTTP)
            assert not missing_negative, f"Expected negative probes were not observed: {sorted(missing_negative)}"
            expected_layouts = {
                (page_name, theme, width)
                for page_name in (
                    "products_list", "bulk_editor", "bulk_review",
                    "single_product_edit", "unmapped_product_edit",
                )
                for theme in ("light", "dark")
                for width in (390, 768, 1280)
            }
            actual_layouts = {
                (row.get("page"), row.get("theme"), row.get("viewport_width"))
                for row in REPORT["layouts"]
            }
            assert len(REPORT["layouts"]) == 30 and actual_layouts == expected_layouts, (
                f"Expected all 30 page/theme/viewport rows, got {len(REPORT['layouts'])}"
            )
            expected_footer_layouts = {
                (theme, width)
                for theme in ("light", "dark")
                for width in (320, 360, 390)
            }
            actual_footer_layouts = {
                (row.get("theme"), row.get("viewport_width"))
                for row in REPORT["footer_mobile_layouts"]
            }
            assert len(REPORT["footer_mobile_layouts"]) == 6 and actual_footer_layouts == expected_footer_layouts, (
                "Expected six sticky-footer rows for 320/360/390px in both themes"
            )
            expected_screenshots = {
                (page_name, theme, width)
                for page_name in (
                    "products_list", "bulk_editor", "bulk_review",
                    "single_product_edit", "unmapped_product_edit",
                )
                for theme in ("light", "dark")
                for width in (390, 1280)
            }
            layout_artifacts = [
                item for item in REPORT["artifacts"]
                if item.get("kind") == "layout_screenshot"
            ]
            actual_screenshots = {
                (item.get("page"), item.get("theme"), item.get("width"))
                for item in layout_artifacts
            }
            if (
                len(layout_artifacts) != 20
                or actual_screenshots != expected_screenshots
                or any(
                    not isinstance(item.get("path"), str)
                    or "/" in item["path"] or "\\" in item["path"]
                    for item in layout_artifacts
                )
            ):
                raise AssertionError("Expected 20 bounded layout screenshots across 5 pages, 2 themes, and 390/1280px")
            assert len(REPORT["checks"]) >= 24, f"Expected at least 24 named browser checks, got {len(REPORT['checks'])}"
            assert REPORT["checks"] and REPORT["interactions"] and REPORT["layouts"]
            REPORT["status"] = "complete"
        except Exception:
            try:
                REPORT["failure_context"] = _bounded_failure_context(page)
            except Exception:
                REPORT["failure_context"] = {"context_error": "unavailable"}
            try:
                ARTIFACTS_PATH.mkdir(parents=True, exist_ok=True)
                screenshot = ARTIFACTS_PATH / "wb-edit-browser-failure.png"
                page.screenshot(path=str(screenshot), full_page=True, timeout=5000)
                REPORT["artifacts"].append({
                    "kind": "failure_screenshot",
                    "path": screenshot.name,
                })
            except Exception as screenshot_error:
                REPORT["artifact_errors"].append({
                    "kind": "failure_screenshot",
                    "error_type": type(screenshot_error).__name__,
                })
            raise
        finally:
            context.close()
            browser.close()


def main() -> int:
    global SERVER, SERVER_THREAD, BASE
    from seller_platform import app
    import seller_platform
    from models import db

    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=True,
        SESSION_COOKIE_SECURE=False,
        MARKETPLACE_OZON_ENABLED=False,
        MARKETPLACE_OZON_PUBLICATION_ENABLED=False,
        MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
    )
    REPORT["source_hashes"] = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in (
            "seller_platform.py",
            "services/wb_media.py",
            "services/product_selection.py",
            "services/wb_edit_review.py",
            "templates/products.html",
            "templates/products_bulk_edit.html",
            "templates/products_bulk_edit_review.html",
            "templates/product_edit.html",
            "tests/ux01/wb_edit_browser.py",
        )
    }
    # Photo URL rendering belongs to WB's CDN. The synthetic browser fixture
    # keeps the page structure while replacing those decorative URLs locally.
    app.jinja_env.globals["wb_photo_url"] = lambda *_args, **_kwargs: "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
    seller_platform.WildberriesAPIClient = FakeWBClient
    seller_platform.wb_photo_url_if_known = lambda *_args, **_kwargs: "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
    fixture = seed_synthetic_wb(app)
    EXPECTED_NEGATIVE_HTTP[
        ("POST", f"/products/{fixture['product_id']}/edit")
    ] = {
        "status": 404,
        "name": "foreign_owner_cannot_edit_seller_product",
    }
    EXPECTED_NEGATIVE_HTTP[
        ("POST", f"/products/{fixture['foreign_product_id']}/edit")
    ] = {
        "status": 404,
        "name": "seller_cannot_edit_foreign_owner_product",
    }

    try:
        SERVER = make_server("127.0.0.1", 0, app, threaded=True)
    except PermissionError as exc:
        REPORT["status"] = "blocked"
        REPORT["blocked_reason"] = f"loopback listener denied by execution policy: {exc}"
        REPORT["checks"].append({
            "name": "loopback_listener_for_flask_playwright_fixture",
            "status": "blocked",
        })
        write_report()
        print(json.dumps({"status": REPORT["status"], "report": str(REPORT_PATH)}))
        return 2
    except OSError as exc:
        REPORT["status"] = "blocked"
        REPORT["blocked_reason"] = f"local browser fixture could not bind its loopback listener: {exc}"
        REPORT["checks"].append({
            "name": "loopback_listener_for_flask_playwright_fixture",
            "status": "blocked",
        })
        write_report()
        print(json.dumps({"status": REPORT["status"], "report": str(REPORT_PATH)}))
        return 2

    BASE = f"http://127.0.0.1:{SERVER.server_port}"
    SERVER_THREAD = threading.Thread(target=SERVER.serve_forever, daemon=True)
    SERVER_THREAD.start()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        if BULK_SCHEMA_RACE_ONLY:
            REPORT["scope"] = "synthetic_wb_bulk_characteristics_schema_race"
        run_browser(app, fixture, bulk_schema_race_only=BULK_SCHEMA_RACE_ONLY)
    except Exception as exc:
        REPORT["status"] = "failed"
        REPORT["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        SERVER.shutdown()
        SERVER_THREAD.join(timeout=5)
        write_report()
        TEMP.cleanup()
    print(json.dumps({"status": REPORT["status"], "report": str(REPORT_PATH)}))
    return 0 if REPORT["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
