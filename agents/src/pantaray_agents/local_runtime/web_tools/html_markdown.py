"""Turn a fetched HTML page into Markdown-like text: headings, lists, links, code.

The page's own ``<main>`` (else ``<article>``, else ``<body>``) is the content.
Only under ``<body>`` is site chrome -- header, footer, navigation, sidebars --
dropped, because inside ``<main>`` those elements carry the article's own title
and callouts.

Nothing in the page may make the text grow faster than the markup: a link keeps
its ``href`` as written (the reader knows the page's URL), nesting indents only a
few levels deep, and a table renders only its own rows.
"""

from __future__ import annotations

import re
from typing import Final

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
_HREF_SCHEME: Final = re.compile(r"^\s*([A-Za-z][A-Za-z0-9+.-]*):")
# Lists and quotes deeper than this are written flush with their parent, so a
# deeply nested page cannot repeat a long prefix on every line.
_MAX_NESTING: Final = 8
_BLOCKS: Final = frozenset(
    "address article aside blockquote body dd details div dl dt figcaption figure "
    "footer form header hr li main ol p pre section summary table ul".split()
) | frozenset(_HEADINGS)


def html_to_markdown(page: str) -> str:
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
    return "\n\n".join(_blocks(root, depth=0))


def _blocks(element: lxml_html.HtmlElement, *, depth: int) -> list[str]:
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
            blocks.extend(_block(child, depth=depth))
        elif isinstance(child.tag, str):
            run.append(_inline(child))
        run.append(_collapse(child.tail))
    flush()
    return blocks


def _block(element: lxml_html.HtmlElement, *, depth: int) -> list[str]:
    tag = element.tag
    if tag in _HEADINGS:
        text = _inline(element).strip()
        return [f"{'#' * _HEADINGS[tag]} {text}"] if text else []
    if tag in {"ul", "ol"}:
        items = [
            item
            for index, child in enumerate(element.findall("li"), start=1)
            if (
                item := _list_item(
                    child, f"{index}. " if tag == "ol" else "- ", depth=depth
                )
            )
        ]
        return ["\n".join(items)] if items else []
    if tag == "pre":
        code = _preformatted(element).strip("\n")
        return [f"```\n{code}\n```"] if code.strip() else []
    if tag == "blockquote":
        quoted = "\n\n".join(_blocks(element, depth=depth + 1))
        prefix = "> " if depth < _MAX_NESTING else ""
        return [_prefix_lines(quoted, prefix)] if quoted else []
    if tag == "table":
        return _table(element)
    if tag == "hr":
        return ["---"]
    return _blocks(element, depth=depth)


def _list_item(item: lxml_html.HtmlElement, marker: str, *, depth: int) -> str:
    content = "\n".join(_blocks(item, depth=depth + 1))
    if not content:
        return ""
    first, *rest = content.split("\n")
    indent = " " * len(marker) if depth < _MAX_NESTING else ""
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
        # A nested table's rows are already in its cell's text.
        if next(row.iterancestors("table")) is table
    ]
    lines = ["| " + " | ".join(cells) + " |" for cells in rows if any(cells)]
    if not lines:
        return []
    header_cells = next(cells for cells in rows if any(cells))
    lines.insert(1, "|" + " --- |" * len(header_cells))
    return ["\n".join(lines)]


def _inline(element: lxml_html.HtmlElement) -> str:
    tag = element.tag
    if tag == "br":
        return "\n"
    text = _collapse(element.text) + "".join(
        (_inline(child) if isinstance(child.tag, str) else "") + _collapse(child.tail)
        for child in element
    )
    if tag == "code":
        return f"`{text.strip()}`" if text.strip() else ""
    if tag == "a":
        label = text.strip()
        href = (element.get("href") or "").strip()
        scheme = _HREF_SCHEME.match(href)
        # An anchor on the same page, or a javascript:/mailto: target, is no
        # page to read next.
        if (
            label
            and href
            and not href.startswith("#")
            and (scheme is None or scheme.group(1).lower() in {"http", "https"})
        ):
            return f"[{label}]({href})"
    return text


def _collapse(text: str | None) -> str:
    return _WHITESPACE.sub(" ", text) if text else ""


def _prefix_lines(text: str, prefix: str) -> str:
    return "\n".join(
        prefix + line if line else prefix.rstrip() for line in text.split("\n")
    )


__all__ = ["html_to_markdown"]
