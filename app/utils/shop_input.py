"""Bounded, normalized plain-text input policies for public shop requests."""
from __future__ import annotations

import html
import re
import unicodedata

import nh3

_PLAIN_TEXT = nh3.Cleaner(tags=set(), attributes={}, clean_content_tags={
    "script", "style", "iframe", "object", "embed", "svg", "math", "template",
}, link_rel=None)
_LINK = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_REPEAT = re.compile(r"(.)\1{20,}", re.DOTALL)


def plain_text(value: str | None, maximum: int, minimum: int = 0, *, multiline: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected text")
    if len(value) > maximum:
        raise ValueError(f"Must contain at most {maximum} characters")
    # Decode nested entities before parsing, so encoded tags meet the same policy.
    while True:
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    value = html.unescape(_PLAIN_TEXT.clean(value)).replace("<", "").replace(">", "")
    value = unicodedata.normalize("NFC", value)
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C") or (multiline and c in "\n\t"))
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"Must contain {minimum} to {maximum} characters")
    return value


def person_name(value: str | None, minimum: int = 1) -> str | None:
    value = plain_text(value, 80, minimum)
    if value is not None and any(not (unicodedata.category(c)[0] in "LM" or c in " '’ʼ.-") for c in value):
        raise ValueError("Name contains unsupported characters")
    if value and not any(unicodedata.category(c).startswith("L") for c in value):
        raise ValueError("Name must contain a letter")
    return value


def email_input(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 254 or any(c in "<>" or unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("Invalid email")
    return value.strip() or None


def phone_input(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 20 or not re.fullmatch(r"\+?[0-9 ()-]+", value):
        raise ValueError("Invalid phone")
    digits = re.sub(r"[^0-9]", "", value)
    if not 10 <= len(digits) <= 15:
        raise ValueError("Phone must contain 10 to 15 digits")
    # Preserve the existing service's country-prefix normalization semantics.
    return value.strip()


def message_input(value: str | None, maximum: int, minimum: int = 0) -> str | None:
    value = plain_text(value, maximum, minimum, multiline=True)
    if value is not None and (_REPEAT.search(value) or len(_LINK.findall(value)) > 2):
        raise ValueError("Message contains excessive repetitions or links")
    return value


def address_unit(value: str | None) -> str | None:
    value = plain_text(value, 20)
    if value is not None and any(not (unicodedata.category(c)[0] in "LMN" or c in " /.,'’ʼ#-№") for c in value):
        raise ValueError("Invalid building or apartment number")
    return value
