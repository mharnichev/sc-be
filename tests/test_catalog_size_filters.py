from types import SimpleNamespace

from app.api.v1.routes.categories import (
    _facet_response,
    _filter_group_slug,
    _matches_selected_filters,
)


def product(values):
    return SimpleNamespace(brand=None, attributes_json={"Розмір": values})


def test_size_facets_merge_equivalent_values_count_products_and_sort_numerically():
    products = [
        product([{"name": "100 мл", "slug": "legacy-100"}, "100ml"]),
        product(" 100\u00a0мл "),
        product("1000 мл"),
        product("125 мл"),
        product("0,5 l"),
        product("500 мл"),
        product("2.5 мл"),
        product("XL"),
    ]
    group = _filter_group_slug("Розмір")
    values = _facet_response(products)[group].values
    assert [(v.name, v.count) for v in values] == [
        ("2.5 мл", 1), ("100 мл", 2), ("125 мл", 1),
        ("500 мл", 2), ("1000 мл", 1), ("XL", 1),
    ]
    selected = next(v.slug for v in values if v.name == "100 мл")
    assert [_matches_selected_filters(p, {group: {selected}}) for p in products] == [
        True, True, False, False, False, False, False, False,
    ]
    assert _matches_selected_filters(products[0], {group: {"legacy-100"}})
    assert _matches_selected_filters(products[0], {group: {"100ml"}})


def test_size_facets_keep_mass_and_volume_separate_and_other_groups_unchanged():
    p = SimpleNamespace(brand=None, attributes_json={
        "Розмір": ["1 kg", "1000 г", "1000 мл", "50 г"],
        "Model": ["100 мл", "100ml"],
    })
    facets = _facet_response([p])
    assert [(v.name, v.count) for v in facets[_filter_group_slug("Розмір")].values] == [
        ("50 г", 1), ("1000 г", 1), ("1000 мл", 1),
    ]
    assert len(facets["model"].values) == 2
