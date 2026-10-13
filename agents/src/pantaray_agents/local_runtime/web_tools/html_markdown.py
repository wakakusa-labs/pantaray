"""Turn a fetched HTML page into Markdown-like text: headings, lists, links, code.

The content is the page's ``<main>``, else its ``role="main"`` region -- both
mark the main content in HTML itself -- else the whole ``<body>``. Nothing is
dropped by guessing which part of a page matters; only elements that carry no
text to read (navigation, scripts, styles, form controls, media) are skipped.
A link keeps its ``href`` as written, since the reader knows the page's URL.

This module is loaded by file path into the page worker, so it imports nothing
but the standard library and lxml.
"""

from __future__ import annotations

import re
from typing import Final

from lxml import html as lxml_html
from lxml.etree import ParserError

# Zero-width spaces are what heading permalink anchors usually render as.
_WHITESPACE: Final = re.compile(r"[\s​]+")
_XML_DECLARATION: Final = re.compile(r"^\s*<\?xml[^>]*\?>")
_NO_TEXT: Final = frozenset(
    "button canvas iframe img input nav noscript object script select style svg "
    "template textarea".split()
)
_HEADINGS: Final = {f"h{level}": level for level in range(1, 7)}
_HREF_SCHEME: Final = re.compile(r"^\s*([A-Za-z][A-Za-z0-9+.-]*):")
_BLOCKS: Final = frozenset(
    "address article aside blockquote body dd details div dl dt figcaption figure "
    "footer form header hr li main ol p pre section summary table ul".split()
) | frozenset(_HEADINGS)


def html_to_markdown(page: str) -> str:
    """The page's readable text, or ``""`` when it has none (a script-built page)."""

    try:
        document = lxml_html.document_fromstring(
            _XML_DECLARATION.sub("", page),
            # Past libxml2's default depth limit the parser silently returns
            # the tree so far, dropping the rest of the page.
            parser=lxml_html.HTMLParser(huge_tree=True),
        )
    except ParserError:
        return ""
    for path in (".//main", ".//*[@role='main']", "body"):
        root = document.find(path)
        if root is not None:
            return "\n\n".join(_blocks(root))
    return ""


def _renders(element: lxml_html.HtmlElement) -> bool:
    """Comments, processing instructions and elements without text render nothing.

    They are passed over here rather than removed from the tree: removing an
    element joins its tail onto the text before it, which costs quadratic time
    when a page repeats one many times.
    """

    return isinstance(element.tag, str) and element.tag not in _NO_TEXT


def _blocks(element: lxml_html.HtmlElement) -> list[str]:
    blocks: list[str] = []
    run: list[str] = [_collapse(element.text)]

    def flush() -> None:
        text = "\n".join(line.strip() for line in "".join(run).split("\n"))
        run.clear()
        if text.strip():
            blocks.append(text.strip())

    for child in element:
        if _renders(child) and child.tag in _BLOCKS:
            flush()
            blocks.extend(_block(child))
        elif _renders(child):
            run.append(_inline(child))
        run.append(_collapse(child.tail))
    flush()
    return blocks


def _block(element: lxml_html.HtmlElement) -> list[str]:
    tag = element.tag
    if tag in _HEADINGS:
        text = _inline(element).strip()
        return [f"{'#' * _HEADINGS[tag]} {text}"] if text else []
    if tag in {"ul", "ol"}:
        items = [
            item
            for index, child in enumerate(element.findall("li"), start=1)
            if _renders(child)
            and (item := _list_item(child, f"{index}. " if tag == "ol" else "- "))
        ]
        return ["\n".join(items)] if items else []
    if tag == "pre":
        code = _preformatted(element).strip("\n")
        return [f"```\n{code}\n```"] if code.strip() else []
    if tag == "blockquote":
        quoted = "\n\n".join(_blocks(element))
        return [_prefix_lines(quoted, "> ")] if quoted else []
    if tag == "table":
        return _table(element)
    if tag == "hr":
        return ["---"]
    return _blocks(element)


def _list_item(item: lxml_html.HtmlElement, marker: str) -> str:
    content = "\n".join(_blocks(item))
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
        elif _renders(child):
            inner = _preformatted(child)
            ends_line = child.tag in _BLOCKS and not inner.endswith("\n")
            parts.append(inner + "\n" if ends_line else inner)
        parts.append(child.tail or "")
    return "".join(parts)


def _table(table: lxml_html.HtmlElement) -> list[str]:
    rows = [
        [
            _collapse(_inline(cell)).strip()
            for cell in row
            if cell.tag in {"td", "th"} and _renders(cell)
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
        (_inline(child) if _renders(child) else "") + _collapse(child.tail)
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
