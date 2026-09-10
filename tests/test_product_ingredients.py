from decimal import Decimal

import pytest

from app.schemas.product import ProductCreate, ProductUpdate
from app.utils.product_ingredients import extract_product_ingredients


@pytest.mark.parametrize("markup", [
    "<h3>Склад товару:</h3><p>Aqua, Glycerin.</p><h3>Характеристики</h3><p>Other text</p>",
    "<p><strong>Склад</strong>: Aqua, Glycerin.</p><p>Купити на нашому сайті.</p>",
    "<p>Спосіб застосування<br>Нанести<br><br><strong>Склад</strong><br>Aqua, Glycerin.</p>",
    "<p><strong>Склад (INCI):</strong><br>Aqua, Glycerin.<br><strong>Характеристики:</strong></p>",
    "Склад:\nAqua, Glycerin.",
])
def test_extracts_only_the_composition(markup: str) -> None:
    assert extract_product_ingredients(markup) == "Aqua, Glycerin."


def test_preserves_batch_note_and_decodes_html() -> None:
    result = extract_product_ingredients(
        "<p><strong>Склад (INCI):</strong><br>Shea Butter, Coconut Oil.<br>"
        "(<em>можливі незначні зміни залежно від партії</em>)</p>"
        "<p><strong>Спосіб застосування:</strong>Нанести.</p>"
    )
    assert result == "Shea Butter, Coconut Oil.\n(можливі незначні зміни залежно від партії)"


def test_selects_matching_variant_not_first_formula() -> None:
    description = (
        "<h3>Classic Shaving Cream</h3><p>Опис</p>"
        "<h3>Склад Deluxe Shaving Cream:</h3><p>Aqua, Deluxe.</p>"
        "<h3>Склад Classic Shaving Cream:</h3><p>Aqua, Classic.</p>"
    )
    assert extract_product_ingredients(description, product_name="Classic Shaving Cream 100 мл") == (
        "Склад Classic Shaving Cream:\nAqua, Classic."
    )


def test_does_not_confuse_whitening_and_smokers_whitening() -> None:
    description = (
        "<h3>Marvis Smokers Whitening Mint</h3><p><strong>Склад:</strong>Aqua, Smokers.</p>"
        "<h3>Marvis Whitening Mint</h3><p><strong>Склад:</strong>Aqua, Whitening.</p>"
    )
    assert extract_product_ingredients(description, product_name="Marvis Whitening Mint 85 мл") == "Aqua, Whitening."


def test_rejects_conflicting_formulas_without_a_positive_product_match() -> None:
    description = (
        "<h3>Склад Classic:</h3><p>Aqua, Classic.</p>"
        "<h3>Склад Deluxe Formula:</h3><p>Aqua, Deluxe.</p>"
    )
    assert extract_product_ingredients(description, product_name="Unrelated Balm") is None
    assert extract_product_ingredients(description) is None


def test_preserves_partial_composition_label() -> None:
    assert extract_product_ingredients("<p>Склад (основні інгредієнти): Aqua, Glycerin.</p>") == (
        "Склад (основні інгредієнти):\nAqua, Glycerin."
    )


def test_key_ingredients_are_qualified_and_full_composition_takes_precedence() -> None:
    partial = "<h3>Основні інгредієнти:</h3><ul><li>Олія ши</li><li>Олія жожоба</li></ul>"
    assert extract_product_ingredients(partial + "<h3>Спосіб застосування:</h3><p>Нанести.</p>") == (
        "Основні інгредієнти:\nОлія ши\nОлія жожоба"
    )
    assert extract_product_ingredients(partial + "<h3>Склад:</h3><p>Aqua, Parfum.</p>") == "Aqua, Parfum."


@pytest.mark.parametrize("description", [
    None, "", "<p>Натуральний склад, доглядає за шкірою.</p>",
    "<p>Потужність складає 6000 об/хв.</p>", "<p>Складна конструкція: сталь.</p>",
    "<h3>Склад:</h3><h3>Спосіб застосування</h3><p>Нанести</p>",
    "<h3>Склад:</h3><p>Aqua.</p><h3>Склад:</h3><p>Alcohol.</p>",
])
def test_does_not_invent_missing_or_ambiguous_composition(description: str | None) -> None:
    assert extract_product_ingredients(description) is None


def test_backoffice_can_set_clear_or_leave_ingredients_unchanged() -> None:
    assert ProductCreate(name="Balm", slug="balm", price=Decimal("350"), ingredients="Shea Butter").ingredients == "Shea Butter"
    assert ProductUpdate(ingredients=None).model_dump(exclude_unset=True) == {"ingredients": None}
    assert "ingredients" not in ProductUpdate(name="Balm").model_dump(exclude_unset=True)
