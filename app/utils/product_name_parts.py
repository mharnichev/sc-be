"""Split supplier product titles into a type, a display name, and package size."""
from __future__ import annotations

from dataclasses import dataclass
import re


PACKAGE_SIZE_PATTERN = re.compile(
    r"(?:[\s,;]+|\s*-\s*|\s*\(\s*)"
    r"(?P<size>(?:\d+(?:[.,]\d+)?\s*[xх×]\s*)?\d+(?:[.,]\d+)?\s*(?:ml|мл|l|л|g|г|гр\.?|kg|кг|шт\.?))"
    r"\s*\)?(?:\s+(?:new|новинка))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProductNameParts:
    old_name: str
    model_name: str | None
    product_type: str | None
    package_size: str | None
    matched_brand: bool


def _brand_markers(brand_name: str | None) -> list[str]:
    if not brand_name:
        return []
    normalized = re.sub(r"[^\w]+", " ", brand_name, flags=re.UNICODE).strip()
    markers = [brand_name]
    if normalized:
        markers.append(normalized)
        markers.append(normalized.split()[0])
    return sorted({marker for marker in markers if len(marker) >= 3}, key=len, reverse=True)


def split_product_name(value: str, *, brand_name: str | None = None) -> ProductNameParts:
    old_name = " ".join(value.split())
    title_without_size = old_name
    package_size = None
    size_match = PACKAGE_SIZE_PATTERN.search(old_name)
    if size_match:
        package_size = _normalize_package_size(size_match.group("size"))
        title_without_size = old_name[:size_match.start()].rstrip(" ,-;")

    for marker in _brand_markers(brand_name):
        match = re.search(re.escape(marker), title_without_size, re.IGNORECASE)
        if match is None:
            continue
        product_type = title_without_size[:match.start()].strip(" ,-;") or None
        name = title_without_size[match.start():].strip()
        if name:
            return ProductNameParts(old_name, name, product_type, package_size, True)

    latin_brand = re.search(r"[A-Za-z][A-Za-z&'’.-]*(?:\s+[A-Za-z][A-Za-z&'’.-]*)?", title_without_size)
    if latin_brand and latin_brand.start() > 0:
        return ProductNameParts(
            old_name,
            title_without_size[latin_brand.start():].strip(),
            title_without_size[:latin_brand.start()].strip(" ,-;") or None,
            package_size,
            False,
        )
    return ProductNameParts(old_name, None, None, package_size, False)


def _normalize_package_size(value: str) -> str:
    size = re.sub(r"\s*[xх×]\s*", " × ", value, flags=re.IGNORECASE)
    size = " ".join(size.split())
    size = re.sub(r"(?i)(?<=\d)(ml|мл|kg|кг|гр\.?|г|l|л|g|шт\.?)$", r" \1", size)
    return re.sub(
        r"(?i)(ml|мл|kg|кг|гр\.?|г|l|л|g|шт\.?)$",
        lambda match: {
            "ml": "мл", "мл": "мл", "kg": "кг", "кг": "кг",
            "гр": "г", "гр.": "г", "г": "г", "l": "л", "л": "л",
            "g": "г", "шт": "шт", "шт.": "шт",
        }[match.group(0).lower()],
        size,
    )
