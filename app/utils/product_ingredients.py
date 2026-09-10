"""Extract explicitly labelled composition from existing catalog descriptions.

This copies source text, including qualifications, without inferring an INCI list.
Descriptions are left intact until the storefront renders ingredients separately.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.parts: list[str] = []
        self.tag = "p"

    def flush(self) -> None:
        text = "\n".join(
            re.sub(r"[\t \xa0\u202f]+", " ", line).strip()
            for line in "".join(self.parts).splitlines()
        ).strip()
        if text:
            self.blocks.append((self.tag, text))
        self.parts = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"p", "div", "li", "ul", "ol", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()
            self.tag = tag
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


_LABEL = re.compile(
    r"^(?P<label>(?:склад\b|состав\b|ingredients\b|INCI\b|інгредієнти\s*\(склад\)|(?:ключові|основні) інгредієнти)"
    r"[^:\n]{0,85})(?::\s*|\n|$)(?P<body>.*)$", re.I | re.S | re.M,
)
_STOP = re.compile(
    r"(?:^|\n)(?:характеристики|спосіб застосування|як застосовувати|опис|переваги|"
    r"активні компоненти|результат|застосування|особливості)\b", re.I,
)
_NOTE = re.compile(r"^\(?\s*(?:можлив|інгредієнти можуть|склад може|примітка|зверніть увагу)", re.I)
_PARTIAL = re.compile(r"^(?:ключові|основні) інгредієнти", re.I)


@dataclass(frozen=True)
class IngredientSection:
    label: str
    text: str
    context: str


def ingredient_sections(description: str | None) -> list[IngredientSection]:
    if not description:
        return []
    parser = _DescriptionParser()
    parser.feed(description)
    parser.flush()
    blocks = parser.blocks
    sections: list[IngredientSection] = []
    context = ""
    for index, (tag, text) in enumerate(blocks):
        match = _LABEL.search(text)
        if match is None:
            if re.fullmatch(r"h[1-6]", tag):
                context = text
            continue
        label = match["label"].strip()
        body = match["body"].strip().lstrip("¦ ")
        # A heading can end before its colon, e.g. <strong>Склад</strong>.
        if not body and index + 1 < len(blocks):
            next_tag, next_text = blocks[index + 1]
            if not re.fullmatch(r"h[1-6]", next_tag) and not _STOP.match(next_text) and not _LABEL.match(next_text):
                body = next_text
                note_index = index + 2
            else:
                continue
        else:
            note_index = index + 1
        if body.endswith(":") and note_index < len(blocks):
            next_tag, next_text = blocks[note_index]
            if not re.fullmatch(r"h[1-6]", next_tag) and not _STOP.match(next_text):
                body += "\n" + next_text
                note_index += 1
        if note_index < len(blocks) and _NOTE.match(blocks[note_index][1]):
            body += "\n" + blocks[note_index][1]
        if _PARTIAL.match(label) and not match["body"].strip():
            while note_index < len(blocks):
                next_tag, next_text = blocks[note_index]
                if re.fullmatch(r"h[1-6]", next_tag) or _STOP.match(next_text) or _LABEL.match(next_text):
                    break
                body += "\n" + next_text
                note_index += 1
        body = _STOP.split(body, maxsplit=1)[0].strip()
        if body:
            sections.append(IngredientSection(label, body, context))
    return sections


def extract_product_ingredients(description: str | None, *, product_name: str = "") -> str | None:
    sections = ingredient_sections(description)
    if not sections:
        return None
    complete = [section for section in sections if not _PARTIAL.match(section.label)]
    sections = complete or sections
    # Some imported descriptions compare multiple products. Prefer the section
    # whose heading identifies this product; never silently use the first formula.
    name = product_name.casefold()
    if len(sections) > 1:
        def score(section: IngredientSection) -> int:
            specific_label = re.sub(r"\bINCI\b", "", section.label, flags=re.I)
            source = specific_label if re.search(r"[a-z]{3}", specific_label, re.I) else section.context
            words = re.findall(r"[a-z]+", source, re.I)
            words = [word.casefold() for word in words if len(word) > 2 and word.casefold() != "inci"]
            return sum(
                len(word) if re.search(r"\b" + re.escape(word) + r"\b", name) else -len(word)
                for word in set(words)
            )

        scores = [score(section) for section in sections]
        best = max(scores)
        if best <= 0 and len({section.text for section in sections}) > 1:
            return None
        matches = [section for section, value in zip(sections, scores) if value == best]
        if len(matches) != 1:
            # Identical repetitions are harmless; conflicting compositions require review.
            if len({section.text for section in matches}) != 1:
                return None
        section = matches[0]
    else:
        section = sections[0]
    # Retain qualifiers such as "key ingredients" or "common composition".
    generic = re.fullmatch(r"(?:склад(?: товару)?|состав|ingredients|INCI)(?:\s*\(INCI\))?", section.label, re.I)
    return section.text if generic else f"{section.label}:\n{section.text}"
