import json

import pytest

from app.utils.backfill_product_names_en import load_names, plan_updates


def source():
    return {1: {"id": 1, "name_original": "Шампунь", "slug": "shampoo", "name_en": "Shampoo", "product_type_en": "Hair Shampoo"}}


def row(**changes):
    return {"id": 1, "name": "Шампунь", "slug": "shampoo", "name_en": None, "product_type_en": None, **changes}


def test_plan_is_idempotent_and_records_previous_values():
    updates = plan_updates([row()], source())
    assert updates == [{"id": 1, "previous": {"name_en": None, "product_type_en": None},
                        "values": {"name_en": "Shampoo", "product_type_en": "Hair Shampoo"}}]
    assert plan_updates([row(name_en="Shampoo", product_type_en="Hair Shampoo")], source()) == []


@pytest.mark.parametrize("rows", [[], [row(), row(id=2)], [row(name="Changed")],
                                   [row(slug="different")], [row(name_en="Manual translation")],
                                   [row(product_type_en="Manual type")]])
def test_catalog_or_source_changes_abort_without_updates(rows):
    with pytest.raises(ValueError):
        plan_updates(rows, source())


def test_load_validates_complete_unique_input(tmp_path):
    path = tmp_path / "names.json"
    payload = {"count": 1, "products": list(source().values())}
    path.write_text(json.dumps(payload))
    assert load_names(path) == source()
    payload["products"].append(payload["products"][0])
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="unique"):
        load_names(path)


@pytest.mark.parametrize("field,value", [("name_en", ""), ("name_en", "x"), ("name_en", "a" * 256),
                                        ("product_type_en", "  Hair Shampoo"), ("id", True)])
def test_invalid_values_rejected_before_database_access(tmp_path, field, value):
    product = list(source().values())[0]
    product[field] = value
    path = tmp_path / "names.json"
    path.write_text(json.dumps({"count": 1, "products": [product]}))
    with pytest.raises(ValueError):
        load_names(path)
