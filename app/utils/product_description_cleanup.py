from __future__ import annotations

import re


_PROMO_LINK = r'<a\b[^>]*\bhref=["\']https?://fjpomades\.com/blog/?["\'][^>]*>\s*'
_PROMO_END = r'<span\b[^>]*>\s*\.\s*(?:&nbsp;|\u00a0)?\s*</span>\s*</p>'
_PRORASO_FJPOMADES_PROMO_RES = (
    re.compile(
        r'<p\b[^>]*>\s*'
        r'<span\b[^>]*>\s*Ще\s+більше\s+про\s+товари\s+Proraso\s+читай\s+у\s*</span>\s*'
        + _PROMO_LINK
        + r'<span\b[^>]*>\s*блозі\s*</span>\s*</a>\s*'
        + _PROMO_END,
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r'<p\b[^>]*>\s*'
        r'(?:<span\b[^>]*>)?\s*Еще\s+больше\s+о\s+товарах\s*(?:&nbsp;|\u00a0|\s)*Proraso\s+читай\s+в\s*(?:</span>)?\s*'
        + _PROMO_LINK
        + r'(?:<span\b[^>]*>)?\s*блоге\s*(?:</span>)?\s*</a>\s*'
        + r'(?:<span\b[^>]*>)?\s*\.\s*(?:&nbsp;|\u00a0)?\s*(?:</span>)?\s*</p>',
        re.IGNORECASE | re.DOTALL,
    ),
)


def remove_proraso_fjpomades_promo(text: str | None) -> str | None:
    """Remove the legacy Proraso/FJ Pomades promo paragraph from a description."""
    if text is None:
        return None
    for pattern in _PRORASO_FJPOMADES_PROMO_RES:
        text = pattern.sub("", text)
    return text
