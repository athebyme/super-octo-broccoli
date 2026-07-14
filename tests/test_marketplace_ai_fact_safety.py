# -*- coding: utf-8 -*-
"""Legacy AI parsing cannot silently manufacture marketplace physical facts."""

import json

from models import SupplierProduct
from services.ai_service import FullProductParsingTask
from services.supplier_service import _build_marketplace_data


def test_full_parse_prompt_requires_explicit_facts_and_provenance():
    task = FullProductParsingTask(client=object())
    system = task.get_system_prompt()
    user = task.build_user_prompt(product_data={
        "title": "Товар без габаритов",
        "description": "Описание без веса",
    })
    assert "ВЫВЕДИ ЛОГИЧЕСКИ" not in system
    assert "оцени вес" not in user
    assert "не придумывай" in system
    assert "field_provenance" in system
    assert "field_provenance" in user

    parsed = task.parse_response(json.dumps({
        "physical": {"weight_g": None},
        "parsing_meta": {"field_provenance": []},
    }))
    assert parsed["parsing_meta"]["field_provenance"] == {}
    assert parsed["parsing_meta"]["extraction_policy"] == "explicit_only"


def test_legacy_wb_projection_has_no_estimated_weight_or_default_package():
    product = SupplierProduct(
        title="Товар без габаритов",
        category="Категория",
        supplier_price=100,
    )
    result = _build_marketplace_data(product, {
        "product_identity": {"product_type": "Тип"},
        "physical": {},
        "package": {},
    })
    assert result["dimensions"] == {
        "length": None,
        "width": None,
        "height": None,
        "weight_kg": None,
    }
    assert result["package_dimensions"] == {
        "length": None,
        "width": None,
        "height": None,
        "weight_kg": None,
    }
