"""HTML fragment policy shared by product writes and legacy response serialization."""

from __future__ import annotations

import nh3


_PRODUCT_HTML = nh3.Cleaner(
    tags={
        "p", "br", "div", "span", "strong", "em", "b", "i", "u", "s",
        "ul", "ol", "li", "h2", "h3", "h4", "h5", "h6", "blockquote",
        "code", "pre", "a",
    },
    attributes={"a": {"href", "title"}},
    clean_content_tags={
        "script", "style", "iframe", "object", "embed", "svg", "math",
        "template", "noscript", "textarea", "title",
    },
    url_schemes={"http", "https", "mailto"},
    link_rel="noopener noreferrer",
    strip_comments=True,
)


def sanitize_product_html(value: str | None) -> str | None:
    """Keep inert product formatting; never trust stored or imported HTML as safe.

    Relative links are permitted by nh3's default URL policy, but executable URL
    schemes, inline styles, event handlers, images and foreign namespaces are not.
    Response sanitization deliberately leaves the underlying ORM record untouched.
    """
    return None if value is None else _PRODUCT_HTML.clean(value)
