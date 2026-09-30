from app.utils.product_description_cleanup import remove_proraso_fjpomades_promo


PROMO = (
    '<p dir="ltr"><span style="color:#c0392b;">Ще більше про товари Proraso читай у </span>'
    '<a href="http://fjpomades.com/blog/"><span style="color:#c0392b;">блозі</span></a>'
    '<span style="color:#c0392b;">.&nbsp;</span></p>'
)


def test_removes_proraso_promo_with_http_and_dir_attribute() -> None:
    description = f"<p>Основний опис.</p>\n{PROMO}\n<p>Наступний абзац.</p>"

    cleaned = remove_proraso_fjpomades_promo(description)

    assert cleaned == "<p>Основний опис.</p>\n\n<p>Наступний абзац.</p>"
    assert "fjpomades.com/blog" not in cleaned


def test_removes_russian_proraso_promo() -> None:
    description = (
        '<p>Описание.</p><p><span style="color:#c0392b;">Еще больше о товарах&nbsp;Proraso читай в '
        '</span><a href="https://fjpomades.com/blog/"><span style="color:#c0392b;">блоге</span></a>'
        '<span style="color:#c0392b;">.</span></p>'
    )

    cleaned = remove_proraso_fjpomades_promo(description)

    assert cleaned == '<p>Описание.</p>'


def test_keeps_other_fj_pomades_content_and_is_idempotent() -> None:
    description = (
        '<p>Посилання на джерело: <a href="https://fjpomades.com/product/">джерело</a>.</p>\n'
        '<p><span style="color:#c0392b;">Ще більше про товари Proraso читай у </span>'
        '<a href="https://fjpomades.com/blog/"><span style="color:#c0392b;">блозі</span></a>'
        '<span style="color:#c0392b;">.&nbsp;</span></p>'
    )

    cleaned = remove_proraso_fjpomades_promo(description)

    assert "https://fjpomades.com/product/" in cleaned
    assert "fjpomades.com/blog" not in cleaned
    assert remove_proraso_fjpomades_promo(cleaned) == cleaned
