from app.utils.product_name_parts import split_product_name


def test_splits_category_brand_and_volume() -> None:
    parts = split_product_name(
        "Крем для гоління Hawkins & Brimble Shaving Cream 100 мл",
        brand_name="Hawkins & Brimble",
    )
    assert parts.old_name == "Крем для гоління Hawkins & Brimble Shaving Cream 100 мл"
    assert parts.product_type == "Крем для гоління"
    assert parts.model_name == "Hawkins & Brimble Shaving Cream"
    assert parts.package_size == "100 мл"
    assert parts.matched_brand is True


def test_splits_weight_and_brand_alias() -> None:
    parts = split_product_name("Бальзам для бороди DapperDan Beard Balm 50ml", brand_name="Dapper Dan")
    assert parts.product_type == "Бальзам для бороди"
    assert parts.model_name == "DapperDan Beard Balm"
    assert parts.package_size == "50 мл"


def test_uses_latin_marker_when_brand_is_missing() -> None:
    parts = split_product_name("Матова паста Morgans Matt Paste 120мл")
    assert parts.product_type == "Матова паста"
    assert parts.model_name == "Morgans Matt Paste"
    assert parts.package_size == "120 мл"


def test_splits_package_size_before_new_and_normalizes_units() -> None:
    parts = split_product_name(
        "Олія перед голінням The BlueBeards Revenge Pre-Shave Oil 100 мл NEW",
        brand_name="The BlueBeards Revenge",
    )

    assert parts.model_name == "The BlueBeards Revenge Pre-Shave Oil"
    assert parts.package_size == "100 мл"


def test_splits_multipack_and_gram_alias() -> None:
    multipack = split_product_name(
        "Інтенсивний догляд за бородою Proraso Hot Oil Beard Treatment WS 4x17ML",
        brand_name="Proraso",
    )
    grams = split_product_name(
        "Бальзам для бороди Reuzel Clean&Fresh Beard Balm 35 гр",
        brand_name="Reuzel",
    )

    assert multipack.model_name == "Proraso Hot Oil Beard Treatment WS"
    assert multipack.package_size == "4 × 17 мл"
    assert grams.model_name == "Reuzel Clean&Fresh Beard Balm"
    assert grams.package_size == "35 г"
