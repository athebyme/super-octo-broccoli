"""Common-content endpoints enforce seller scope, CSRF integration and typed bodies."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import AgentChangeSnapshot, ImportedProduct, Seller, User, db
from routes.common_product_content import register_common_product_content_routes


@pytest.fixture
def api():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="common-content-route-test",
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        WTF_CSRF_ENABLED=False,
    )
    db.init_app(app)
    LoginManager(app)
    CSRFProtect(app)
    register_common_product_content_routes(app)
    with app.app_context():
        db.create_all()
        user = User(username="common-route-user", email="common-route@test.local")
        user.set_password("synthetic")
        seller = Seller(user=user, company_name="Route seller")
        db.session.add(seller)
        db.session.flush()
        product = ImportedProduct(
            seller_id=seller.id,
            external_id="route-product",
            source_type="csv",
            title="Исходное название",
            description="Исходное описание",
            original_data='{"title":"Исходное название","description":"Исходное описание"}',
        )
        foreign_user = User(username="foreign-route-user", email="foreign-route@test.local")
        foreign_user.set_password("synthetic")
        foreign_seller = Seller(user=foreign_user, company_name="Foreign seller")
        db.session.add_all([product, foreign_seller])
        db.session.flush()
        foreign = ImportedProduct(
            seller_id=foreign_seller.id,
            external_id="foreign-route-product",
            title="Чужой товар",
        )
        db.session.add(foreign)
        db.session.commit()
        client = app.test_client()
        ids = (seller.id, user.id, product.id, foreign.id)
        yield app, client, ids
        db.session.remove()
        db.drop_all()


def _auth(seller_id, user_id):
    user = SimpleNamespace(
        id=user_id,
        seller=SimpleNamespace(id=seller_id),
        is_authenticated=True,
        is_active=True,
        is_admin=False,
    )
    return (
        patch("routes.common_product_content.current_user", user),
        patch("flask_login.utils._get_user", return_value=user),
    )


def test_get_is_seller_scoped_and_returns_effective_inherited_contract(api):
    app, client, (seller_id, user_id, own_id, foreign_id) = api
    user_patch, login_patch = _auth(seller_id, user_id)
    with user_patch, login_patch:
        own = client.get(f"/api/my-products/{own_id}/common-content")
        foreign = client.get(f"/api/my-products/{foreign_id}/common-content")
        missing = client.get("/api/my-products/999999/common-content")

    assert own.status_code == 200
    product = own.get_json()["product"]
    assert product["fields"]["title"]["effective"] == "Исходное название"
    assert product["fields"]["title"]["inherited"] == "Исходное название"
    assert own.get_json()["save_effect"] == "common_only"
    assert foreign.status_code == 404
    assert "Чужой товар" not in foreign.get_data(as_text=True)
    assert missing.status_code == 404


def test_editor_json_bootstrap_negotiates_explicit_json_and_is_private_no_store(api):
    app, client, (seller_id, user_id, own_id, _) = api
    app.add_url_rule("/my-products", endpoint="seller_my_products", view_func=lambda: "catalog")
    app.add_url_rule("/my-products/beta", endpoint="seller_my_products_beta", view_func=lambda: "beta")
    user_patch, login_patch = _auth(seller_id, user_id)
    rendered = {}

    def fake_render_template(template, **context):
        rendered["template"] = template
        rendered.update(context)
        return "html editor"

    with user_patch, login_patch, patch(
        "routes.common_product_content.render_template",
        side_effect=fake_render_template,
    ):
        html_tie = client.get(
            f"/my-products/common-content?product_id={own_id}",
            headers={"Accept": "text/html, application/json"},
        )
        json_response = client.get(
            f"/my-products/common-content?product_id={own_id}",
            headers={"Accept": "application/json, text/html;q=0.8"},
        )

    assert html_tie.status_code == 200
    assert html_tie.get_data(as_text=True) == "html editor"
    assert rendered["template"] == "common_product_content.html"
    assert rendered["common_content_bootstrap"]["selectedProductIds"] == [own_id]
    assert "Accept" in html_tie.vary

    assert json_response.status_code == 200
    payload = json_response.get_json()
    assert set(payload) == {
        "success", "products", "selectedProductIds", "csrfToken", "previewUrl",
        "applyUrl", "readUrlBase", "workspaceUrl", "workspaceOwner",
    }
    assert payload["success"] is True
    assert payload["selectedProductIds"] == [own_id]
    assert [product["product_id"] for product in payload["products"]] == [own_id]
    assert payload["csrfToken"]
    assert payload["csrfToken"] == rendered["common_content_bootstrap"]["csrfToken"]
    assert payload["previewUrl"] == rendered["common_content_bootstrap"]["previewUrl"]
    assert payload["applyUrl"] == rendered["common_content_bootstrap"]["applyUrl"]
    assert payload["workspaceUrl"] == "/my-products/beta"
    assert payload["workspaceOwner"] == f"{user_id}:{seller_id}"
    assert json_response.headers["Cache-Control"] == "private, no-store"
    assert "Accept" in json_response.vary


def test_json_editor_selection_errors_are_private_no_store(api):
    app, client, (seller_id, user_id, own_id, foreign_id) = api
    app.add_url_rule("/my-products", endpoint="seller_my_products", view_func=lambda: "catalog")
    app.add_url_rule("/my-products/beta", endpoint="seller_my_products_beta", view_func=lambda: "beta")
    user_patch, login_patch = _auth(seller_id, user_id)
    accept_json = {"Accept": "application/json"}
    with user_patch, login_patch:
        responses = [
            client.get("/my-products/common-content?product_id=not-an-id", headers=accept_json),
            client.get(
                f"/my-products/common-content?product_id={own_id}&product_id={own_id}",
                headers=accept_json,
            ),
            client.get(
                "/my-products/common-content?" + "&".join("product_id=" + str(index + 1) for index in range(51)),
                headers=accept_json,
            ),
            client.get(f"/my-products/common-content?product_id={foreign_id}", headers=accept_json),
        ]

    assert [(response.status_code, response.get_json()["code"]) for response in responses] == [
        (400, "invalid_selection"),
        (400, "duplicate_selection"),
        (413, "too_many_items"),
        (404, "common_content_not_found"),
    ]
    assert all(response.headers["Cache-Control"] == "private, no-store" for response in responses)
    assert all("Accept" in response.vary for response in responses)


def test_json_editor_missing_seller_error_is_private_no_store(api):
    app, client, (_, user_id, _, _) = api
    missing_seller = SimpleNamespace(
        id=user_id,
        seller=None,
        is_authenticated=True,
        is_active=True,
        is_admin=False,
    )
    with patch("routes.common_product_content.current_user", missing_seller), patch(
        "flask_login.utils._get_user", return_value=missing_seller,
    ):
        response = client.get(
            "/my-products/common-content",
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 404
    assert response.get_json() == {
        "success": False,
        "error": "Для этой операции нужен кабинет продавца",
        "code": "seller_required",
    }
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "Accept" in response.vary


def test_preview_apply_uses_signed_review_and_server_actor_audit(api):
    app, client, (seller_id, user_id, product_id, _) = api
    user_patch, login_patch = _auth(seller_id, user_id)
    with user_patch, login_patch:
        preview = client.post(
            "/api/my-products/common-content/preview",
            json={"items": [{
                "product_id": product_id,
                "expected_content_edit_version": 1,
                "changes": {"title": {"mode": "override", "value": "Сохранённое название"}},
                "recipients": [],
            }]},
        )
        assert preview.status_code == 200
        payload = preview.get_json()["preview"]
        assert payload["expires_in_seconds"] == 600
        assert payload["items"][0]["fields"][0]["after"] == "Сохранённое название"
        applied = client.post(
            "/api/my-products/common-content/apply",
            json={"preview_token": payload["preview_token"]},
        )
        current = client.get(f"/api/my-products/{product_id}/common-content")

    assert applied.status_code == 200
    assert applied.get_json()["applied"][0]["changed_fields"] == ["title"]
    assert "Черновики и опубликованные карточки не изменены" in applied.get_json()["notice"]
    assert current.get_json()["product"]["fields"]["title"]["is_overridden"] is True
    with app.app_context():
        snapshot = AgentChangeSnapshot.query.one()
        assert snapshot.task_id is None
        assert snapshot.agent_id == "seller-common-content-v1"
        assert json_value(snapshot.new_values)["__seller_common_content_audit"]["actor_user_id"] == user_id


def json_value(value):
    import json
    return json.loads(value)


@pytest.mark.parametrize("body", [
    {"items": [], "seller_id": 1},
    {"items": "not-a-list"},
    {"items": [{"product_id": 1, "expected_content_edit_version": True, "changes": {"title": {"mode": "override", "value": "X"}}, "recipients": []}]},
])
def test_preview_rejects_unknown_fields_and_untyped_values(api, body):
    _, client, (seller_id, user_id, _, _) = api
    user_patch, login_patch = _auth(seller_id, user_id)
    with user_patch, login_patch:
        response = client.post("/api/my-products/common-content/preview", json=body)
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_apply_body_and_preview_token_are_strict(api):
    _, client, (seller_id, user_id, _, _) = api
    user_patch, login_patch = _auth(seller_id, user_id)
    with user_patch, login_patch:
        extra = client.post(
            "/api/my-products/common-content/apply",
            json={"preview_token": "x", "seller_id": seller_id},
        )
        invalid = client.post(
            "/api/my-products/common-content/apply",
            json={"preview_token": "not-signed"},
        )
    assert extra.status_code == 400
    assert invalid.status_code == 400


def test_page_selection_parser_rejects_duplicate_and_foreign_ids_before_rendering(api):
    _, client, (seller_id, user_id, own_id, foreign_id) = api
    user_patch, login_patch = _auth(seller_id, user_id)
    with user_patch, login_patch:
        duplicate = client.get(f"/my-products/common-content?product_id={own_id}&product_id={own_id}")
        foreign = client.get(f"/my-products/common-content?product_id={foreign_id}")
        invalid = client.get("/my-products/common-content?product_id=1&seller_id=2")
        overflow = client.get("/my-products/common-content?product_id=9223372036854775808")
        overlong = client.get("/my-products/common-content?product_id=" + ("9" * 5000))
    assert duplicate.status_code == 400
    assert foreign.status_code == 404
    assert invalid.status_code == 400
    assert overflow.status_code == 400
    assert overlong.status_code == 400
