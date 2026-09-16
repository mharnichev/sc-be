"""Booking-only aliases for normalized Ukrainian local and international phones."""


def canonical_booking_phone(normalized: str) -> str:
    if normalized.startswith("+0") and len(normalized) == 11 and normalized[1:].isdigit():
        return "+38" + normalized[1:]
    return normalized


def phone_aliases(normalized: str) -> set[str]:
    canonical = canonical_booking_phone(normalized)
    aliases = {normalized, canonical}
    if canonical.startswith("+380") and len(canonical) == 13 and canonical[1:].isdigit():
        aliases.add("+" + canonical[3:])
    return aliases
