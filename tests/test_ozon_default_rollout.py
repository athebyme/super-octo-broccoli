"""Manual Ozon upload stays default-on while autonomous writes stay dark."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_application_and_deployment_defaults_enable_manual_ozon_upload():
    application = (ROOT / "seller_platform.py").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "os.environ.get('MARKETPLACE_OZON_ENABLED', '1')" in application
    assert (
        "os.environ.get('MARKETPLACE_OZON_PUBLICATION_ENABLED', '1')"
        in application
    )
    assert (
        "MARKETPLACE_OZON_ENABLED=${MARKETPLACE_OZON_ENABLED:-1}"
        in compose
    )
    assert (
        "MARKETPLACE_OZON_PUBLICATION_ENABLED="
        "${MARKETPLACE_OZON_PUBLICATION_ENABLED:-1}"
        in compose
    )
    assert "\nMARKETPLACE_OZON_ENABLED=1\n" in example
    assert "\nMARKETPLACE_OZON_PUBLICATION_ENABLED=1\n" in example


def test_autonomous_and_commercial_ozon_writes_remain_explicit_opt_in():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert (
        "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED="
        "${MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED:-0}"
        in compose
    )
    assert (
        "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED="
        "${MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED:-0}"
        in compose
    )
    assert "\nMARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=0\n" in example
    assert "\nMARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=0\n" in example


def test_runbook_matches_default_on_manual_upload_and_durable_bulk_flow():
    runbook = (
        ROOT / "docs" / "OZON_PRODUCTION_RUNBOOK.md"
    ).read_text(encoding="utf-8")
    plan = (
        ROOT / "docs" / "OZON_MARKETPLACE_IMPLEMENTATION_PLAN.md"
    ).read_text(encoding="utf-8")

    assert "| `MARKETPLACE_OZON_ENABLED` | `1` |" in runbook
    assert "| `MARKETPLACE_OZON_PUBLICATION_ENABLED` | `1` |" in runbook
    assert "/marketplaces/ozon/uploads/" in runbook
    assert "manual Ozon" in plan
    assert "default-on" in plan
