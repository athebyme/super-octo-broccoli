"""Parser-owned source evidence is limited to direct, typed supplier cells."""

import csv
import json
from io import StringIO
from types import SimpleNamespace

from models import SupplierProduct
from services.supplier_service import (
    SupplierCSVParser,
    _copy_to_imported_product,
    _update_supplier_product,
)
from services.smart_product_parser import supplier_material_input_fingerprint


def _csv(rows):
    stream = StringIO()
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue()


def _supplier(*, code="observations-test", mapping=None, has_header=True):
    return SimpleNamespace(
        code=code,
        csv_column_mapping=mapping,
        csv_has_header=has_header,
        csv_delimiter=";",
        csv_encoding="utf-8",
    )


def _observed_mapping():
    return {
        "external_id": {"column": "id", "type": "string"},
        "title": {"column": "title", "type": "string"},
        "brand": {"column": "brand", "type": "string"},
        "country": {"column": "country", "type": "string"},
        "colors": {"column": "colors", "type": "list", "separator": "|"},
        "description": {"column": "description", "type": "string"},
        # These configuration-mapped values attempt to forge the reserved keys.
        "observed_supplier_fields_version": {"column": "spoof_version", "type": "number"},
        "observed_supplier_fields": {"column": "spoof_fields", "type": "string"},
        "_include_unmapped": True,
    }


def test_mapped_cells_survive_enrichment_without_promoting_inference_or_forgery():
    headers = [
        "id", "title", "brand", "country", "colors", "description",
        "spoof_version", "spoof_fields",
        "observed_supplier_fields_version", "observed_supplier_fields",
    ]
    forged = json.dumps({"country": "Подделка", "colors": ["не из CSV"]}, ensure_ascii=False)
    rows = [
        headers,
        ["direct", "Прямые значения", "Обычная марка", "Китай", "красный|синий",
         "Описание прямых значений без извлечений.", "77", forged, "88", forged],
        ["color-only", "Только прямой цвет", "Обычная марка", "", "фуксия",
         "Описание без извлечений.", "77", forged, "88", forged],
        ["description", "Значения из описания", "", "", "",
         "Страна: Испания. Цвет: голубой.", "77", forged, "88", forged],
        ["brand", "Значение из бренда", "satisfyer", "", "",
         "Обычное описание товара для проверки.", "77", forged, "88", forged],
        ["oversize", "Слишком длинные значения", "", "К" * 121,
         "|".join("оттенок" + str(index) for index in range(17)),
         "Обычное описание товара для проверки.", "77", forged, "88", forged],
        ["controls", "Управляющие символы", "", "Ки\x85тай", "крас\x01ный",
         "Обычное описание товара для проверки.", "77", forged, "88", forged],
    ]
    parser = SupplierCSVParser(_supplier(mapping=_observed_mapping()))
    products = parser.parse_and_normalize(_csv(rows))
    by_id = {product["external_id"]: product for product in products}

    direct = by_id["direct"]
    assert direct["observed_supplier_fields_version"] == 1
    assert direct["observed_supplier_fields"] == {
        "country": "Китай",
        "colors": ["красный", "синий"],
    }
    assert direct["raw_extra"]["observed_supplier_fields_version"] == "88"
    assert json.loads(direct["raw_extra"]["observed_supplier_fields"]) == json.loads(forged)

    color_only = by_id["color-only"]
    assert color_only["observed_supplier_fields_version"] == 1
    assert color_only["observed_supplier_fields"] == {"colors": ["фуксия"]}

    described = by_id["description"]
    assert described["country"] == "Испания"
    assert described["colors"]
    assert "observed_supplier_fields_version" not in described
    assert "observed_supplier_fields" not in described

    branded = by_id["brand"]
    assert branded["country"] == "Германия"
    assert "observed_supplier_fields_version" not in branded
    assert "observed_supplier_fields" not in branded

    oversized = by_id["oversize"]
    assert "observed_supplier_fields_version" not in oversized
    assert "observed_supplier_fields" not in oversized
    assert len(oversized["country"]) == 121
    assert len(oversized["colors"]) == 17

    controls = by_id["controls"]
    assert "observed_supplier_fields_version" not in controls
    assert "observed_supplier_fields" not in controls

    supplier_product = SupplierProduct(supplier_id=1)
    _update_supplier_product(supplier_product, direct)
    stored_snapshot = json.loads(supplier_product.original_data_json)
    assert stored_snapshot["observed_supplier_fields_version"] == 1
    assert stored_snapshot["observed_supplier_fields"] == direct["observed_supplier_fields"]
    without_marker = {
        key: value for key, value in direct.items()
        if key not in {"observed_supplier_fields_version", "observed_supplier_fields"}
    }
    assert supplier_material_input_fingerprint(direct) == supplier_material_input_fingerprint(without_marker)

    copied_product = _copy_to_imported_product(seller_id=1, sp=supplier_product)
    copied_snapshot = json.loads(copied_product.original_data)
    assert copied_snapshot["observed_supplier_fields_version"] == 1
    assert copied_snapshot["observed_supplier_fields"] == direct["observed_supplier_fields"]


def test_generic_country_alias_and_description_inference_are_not_attested():
    parser = SupplierCSVParser(_supplier(mapping=None))
    products = parser.parse_and_normalize(_csv([
        ["id", "Название", "Страна", "Описание"],
        ["generic", "Generic item", "Китай", "Цвет: фиолетовый. Страна производства: Германия."],
    ]))
    product = products[0]
    assert product["country"] == "Китай"
    assert product["colors"]
    assert "observed_supplier_fields_version" not in product
    assert "observed_supplier_fields" not in product


def test_sexoptovik_legacy_country_and_colors_are_direct_source_observations():
    row = [""] * 18
    row[0] = "123"
    row[2] = "Legacy product"
    row[5] = "Китай"
    row[9] = "красный, синий"
    products = SupplierCSVParser(
        _supplier(code="sexoptovik", mapping=None, has_header=False)
    ).parse_and_normalize(_csv([row]))

    assert len(products) == 1
    assert products[0]["observed_supplier_fields_version"] == 1
    assert products[0]["observed_supplier_fields"] == {
        "country": "Китай",
        "colors": ["красный", "синий"],
    }
