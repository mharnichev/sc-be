from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
import re
import unicodedata


VOLUME_PATTERN = re.compile(
    r"(?<![\d.,])(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ml|мл|l|л)(?![A-Za-zА-Яа-яІіЇїЄє])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProductVolumeMetadata:
    volume_ml: int | None = None
    variant_group_key: str | None = None


def normalize_variant_text(value: object | None) -> str:
    return "" if value is None else str(value).strip()


def extract_volume_ml(name: str, size: str | None = None) -> int | None:
    for source in (normalize_variant_text(size), normalize_variant_text(name)):
        matches = list(VOLUME_PATTERN.finditer(source))
        if not matches:
            continue
        match = matches[-1]
        numeric_value = float(match.group("value").replace(",", "."))
        unit = match.group("unit").casefold()
        volume_ml = round(numeric_value * 1000) if unit in {"l", "л"} else round(numeric_value)
        return volume_ml if volume_ml > 0 else None
    return None


def canonical_volume_product_name(name: str) -> str:
    without_volume = VOLUME_PATTERN.sub(" ", unicodedata.normalize("NFKC", name))
    normalized = re.sub(r"[\s|/_,;:(){}\[\]-]+", " ", without_volume.casefold())
    return " ".join(normalized.split()).strip(" .")


def build_variant_group_key(brand_name: str | None, canonical_name: str) -> str:
    identity = f"{normalize_variant_text(brand_name).casefold()}|{canonical_name}"
    return sha256(identity.encode("utf-8")).hexdigest()


def build_product_volume_metadata(rows: list[dict[str, object]]) -> dict[str, ProductVolumeMetadata]:
    candidates: dict[str, tuple[str, str, int]] = {}
    grouped: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)

    for row in rows:
        sku = normalize_variant_text(row.get("Артикул"))
        if not sku:
            continue
        name = normalize_variant_text(row.get("Название модификации (UA)")) or normalize_variant_text(
            row.get("Название (UA)")
        )
        volume_ml = extract_volume_ml(name, normalize_variant_text(row.get("Размер (UA)")))
        if not name or volume_ml is None:
            continue
        brand_name = normalize_variant_text(row.get("Бренд"))
        canonical_name = canonical_volume_product_name(name)
        if not canonical_name:
            continue
        candidates[sku] = (brand_name, canonical_name, volume_ml)
        grouped[(brand_name.casefold(), canonical_name)].append((sku, volume_ml))

    metadata = {
        sku: ProductVolumeMetadata(volume_ml=volume_ml)
        for sku, (_brand_name, _canonical_name, volume_ml) in candidates.items()
    }
    for (_normalized_brand, canonical_name), items in grouped.items():
        if len({volume_ml for _sku, volume_ml in items}) < 2:
            continue
        brand_name = candidates[items[0][0]][0]
        group_key = build_variant_group_key(brand_name, canonical_name)
        for sku, volume_ml in items:
            metadata[sku] = ProductVolumeMetadata(
                volume_ml=volume_ml,
                variant_group_key=group_key,
            )
    return metadata
