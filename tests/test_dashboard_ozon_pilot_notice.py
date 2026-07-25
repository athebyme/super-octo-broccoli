"""The retired Ozon pilot announcement does not linger after rollout."""

from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"
).read_text(encoding="utf-8")


def test_ozon_pilot_notice_is_removed_after_default_on_rollout():
    assert "ozon_pilot_notice_v1" not in TEMPLATE
    assert "Поддержка Ozon работает в пилотном режиме" not in TEMPLATE
    assert 'id="ozon-pilot-notice"' not in TEMPLATE
