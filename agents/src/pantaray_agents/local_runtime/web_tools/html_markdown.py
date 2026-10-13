"""Turn a fetched HTML page into Markdown-like text: headings, lists, links, code.

The page's own ``<main>`` (else ``<article>``, else ``<body>``) is the content.
Only under ``<body>`` is site chrome -- header, footer, navigation, sidebars --
dropped, because inside ``<main>`` those elements carry the article's own title
and callouts.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urljoin, urlsplit

from lxml import html as lxml_html
from lxml.etree import ParserError

# Zero-width spaces are what heading permalink anchors usually render as.
_WHITESPACE: Final = re.compile(r"[\s\u200b]+")
_XML_DECLARATION: Final = re.compile(r"^\s*<\?xml[^>]*\?>")
_NEVER_TEXT: Final = frozenset(
    "button canvas iframe img input nav noscript object script select style svg "
    "template textarea".split()
)
_SITE_CHROME: Final = frozenset({"aside", "footer", "header"})
_HEADINGS: Final = {f"h{level}": level for level in range(1, 7)}
_BLOCKS: Final = frozenset(
    "address article aside blockquote body dd details div dl dt figcaption figure "
    "footer form header hr li main ol p pre section summary table ul".split()
) | frozenset(_HEADINGS)


def html_to_markdown(page: str, *, base_url: str) -> str:
    """The page's readable text, or ``""`` when it has none (a script-built page)."""

    try:
        document = lxml_html.document_fromstring(_XML_DECLARATION.sub("", page))
    except ParserError:
        return ""
    root = document.find(".//main")
    if root is None:
        root = document.find(".//article")
    if root is None:
        root = document.find("body")
    if root is None:
        return ""
    dropped = _NEVER_TEXT | (_SITE_CHROME if root.tag == "body" else frozenset())
    for element in list(root.iter()):
        if element is root or not isinstance(element.tag, str):
            continue
        if element.tag in dropped:
            element.drop_tree()
    return "\n\n".join(_blocks(root, base_url=base_url))


def _blocks(element: lxml_html.HtmlElement, *, base_url: str) -> list[str]:
    blocks: list[str] = []
    run: list[str] = [_collapse(element.text)]

    def flush() -> None:
        text = "\n".join(line.strip() for line in "".join(run).split("\n"))
        run.clear()
        if text.strip():
            blocks.append(text.strip())

    for child in element:
        if isinstance(child.tag, str) and child.tag in _BLOCKS:
            flush()
            blocks.extend(_block(child, base_url=base_url))
        elif isinstance(child.tag, str):
            run.append(_inline(child, base_url=base_url))
        run.append(_collapse(child.tail))
    flush()
    return blocks


def _block(element: lxml_html.HtmlElement, *, base_url: str) -> list[str]:
    tag = element.tag
    if tag in _HEADINGS:
        text = _inline(element, base_url=base_url).strip()
        return [f"{'#' * _HEADINGS[tag]} {text}"] if text else []
    if tag in {"ul", "ol"}:
        items = [
            item
            for index, child in enumerate(element.findall("li"), start=1)
            if (
                item := _list_item(
                    child, f"{index}. " if tag == "ol" else "- ", base_url
                )
            )
        ]
        return ["\n".join(items)] if items else []
    if tag == "pre":
        code = _preformatted(element).strip("\n")
        return [f"```\n{code}\n```"] if code.strip() else []
    if tag == "blockquote":
        quoted = "\n\n".join(_blocks(element, base_url=base_url))
        return [_prefix_lines(quoted, "> ")] if quoted else []
    if tag == "table":
        return _table(element)
    if tag == "hr":
        return ["---"]
    return _blocks(element, base_url=base_url)


def _list_item(item: lxml_html.HtmlElement, marker: str, base_url: str) -> str:
    content = "\n".join(_blocks(item, base_url=base_url))
    if not content:
        return ""
    first, *rest = content.split("\n")
    indent = " " * len(marker)
    return "\n".join(
        [marker + first, *(indent + line if line else "" for line in rest)]
    )


def _preformatted(element: lxml_html.HtmlElement) -> str:
    """Code as laid out, where highlighters put each line in its own block."""

    parts = [element.text or ""]
    for child in element:
        if child.tag == "br":
            parts.append("\n")
        elif isinstance(child.tag, str):
            inner = _preformatted(child)
            ends_line = child.tag in _BLOCKS and not inner.endswith("\n")
            parts.append(inner + "\n" if ends_line else inner)
        parts.append(child.tail or "")
    return "".join(parts)


def _table(table: lxml_html.HtmlElement) -> list[str]:
    rows = [
        [
            _collapse(cell.text_content()).strip()
            for cell in row
            if cell.tag in {"td", "th"}
        ]
        for row in table.iter("tr")
    ]
    lines = ["| " + " | ".join(cells) + " |" for cells in rows if any(cells)]
    if not lines:
        return []
    header_cells = next(cells for cells in rows if any(cells))
    lines.insert(1, "|" + " --- |" * len(header_cells))
    return ["\n".join(lines)]


def _inline(element: lxml_html.HtmlElement, *, base_url: str) -> str:
    tag = element.tag
    if tag == "br":
        return "\n"
    text = _collapse(element.text) + "".join(
        (_inline(child, base_url=base_url) if isinstance(child.tag, str) else "")
        + _collapse(child.tail)
        for child in element
    )
    if tag == "code":
        return f"`{text.strip()}`" if text.strip() else ""
    if tag == "a":
        label = text.strip()
        href = urljoin(base_url, element.get("href") or "")
        target = urlsplit(href)
        if (
            label
            and target.scheme in {"http", "https"}
            and not _same_page(href, base_url)
        ):
            return f"[{label}]({href})"
    return text


def _same_page(href: str, base_url: str) -> bool:
    return href.split("#", 1)[0] == base_url.split("#", 1)[0]


def _collapse(text: str | None) -> str:
    return _WHITESPACE.sub(" ", text) if text else ""


def _prefix_lines(text: str, prefix: str) -> str:
    return "\n".join(
        prefix + line if line else prefix.rstrip() for line in text.split("\n")
    )


__all__ = ["html_to_markdown"]
