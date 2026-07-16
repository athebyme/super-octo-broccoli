"""The dashboard Ozon pilot notice is explicit and dismissible."""

from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"
).read_text(encoding="utf-8")


def test_ozon_pilot_notice_is_seller_only_and_cookie_gated():
    assert "current_user.seller and request.cookies.get('ozon_pilot_notice_v1')" in TEMPLATE
    assert "Поддержка Ozon работает в пилотном режиме" in TEMPLATE
    assert "url_for('marketplace_accounts.index')" in TEMPLATE


def test_ozon_pilot_notice_dismissal_is_accessible_and_persistent():
    assert 'id="ozon-pilot-dismiss"' in TEMPLATE
    assert 'aria-label="Скрыть объявление о пилоте Ozon"' in TEMPLATE
    assert "ozon_pilot_notice_v1=dismissed" in TEMPLATE
    assert "SameSite=Lax" in TEMPLATE
