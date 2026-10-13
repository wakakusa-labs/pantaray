"""Turn a fetched HTML page into Markdown-like text: headings, lists, links, code.

The page's own ``<main>`` (else its ``role="main"`` region, else its only
``<article>``, else ``<body>``) is the content.
Only under ``<body>`` is site chrome -- header, footer, navigation, sidebars --
dropped, because inside ``<main>`` those elements carry the article's own title
and callouts.

Nothing in the page may make the text grow faster than the markup: a link keeps
its ``href`` as written (the reader knows the page's URL), nesting indents only a
few levels deep, and a table renders only its own rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        document = lxml_html.document_fromstring(
            _XML_DECLARATION.sub("", page),
            # Past libxml2's default depth limit the parser silently returns
            # the tree so far, dropping the rest of the page as if it were not
            # there. A tree deeper than rendering can recurse fails the page.
            parser=lxml_html.HTMLParser(huge_tree=True),
        )
    except ParserError:
        return ""
    root = _content_root(document)
    if root is None:
        return ""
    # Skipped while rendering rather than removed from the tree: removing an
    # element joins its tail onto the text before it, which costs quadratic
    # time when a page repeats a skipped element many times.
    renderer = _Renderer(
        skipped=_NEVER_TEXT | (_SITE_CHROME if root.tag == "body" else frozenset())
    )
    return "\n\n".join(renderer.blocks(root, depth=0))


def _content_root(document: lxml_html.HtmlElement) -> lxml_html.HtmlElement | None:
    """The page's main region; a list of several articles is read whole."""

    for path in (".//main", ".//*[@role='main']"):
        region = document.find(path)
        if region is not None:
            return region
    articles = document.findall(".//article")
    if len(articles) == 1:
        return articles[0]
    return document.find("body")


@dataclass(frozen=True, slots=True)
class _Renderer:
    skipped: frozenset[str]

    def _renders(self, element: lxml_html.HtmlElement) -> bool:
        """Comments, processing instructions and skipped tags render nothing."""

        return isinstance(element.tag, str) and element.tag not in self.skipped

    def blocks(self, element: lxml_html.HtmlElement, *, depth: int) -> list[str]:
        blocks: list[str] = []
        run: list[str] = [_collapse(element.text)]

        def flush() -> None:
            text = "\n".join(line.strip() for line in "".join(run).split("\n"))
            run.clear()
            if text.strip():
                blocks.append(text.strip())

        for child in element:
            if self._renders(child) and child.tag in _BLOCKS:
                flush()
                blocks.extend(self._block(child, depth=depth))
            elif self._renders(child):
                run.append(self.inline(child))
            run.append(_collapse(child.tail))
        flush()
        return blocks

    def _block(self, element: lxml_html.HtmlElement, *, depth: int) -> list[str]:
        tag = element.tag
        if tag in _HEADINGS:
            text = self.inline(element).strip()
            return [f"{'#' * _HEADINGS[tag]} {text}"] if text else []
        if tag in {"ul", "ol"}:
            items = [
                item
                for index, child in enumerate(element.findall("li"), start=1)
                if self._renders(child)
                and (
                    item := self._list_item(
                        child, f"{index}. " if tag == "ol" else "- ", depth=depth
                    )
                )
            ]
            return ["\n".join(items)] if items else []
        if tag == "pre":
            code = self._preformatted(element).strip("\n")
            return [f"```\n{code}\n```"] if code.strip() else []
        if tag == "blockquote":
            quoted = "\n\n".join(self.blocks(element, depth=depth + 1))
            if not quoted or depth >= _MAX_NESTING:
                return [quoted] if quoted else []
            return [_prefix_lines(quoted, "> ")]
        if tag == "table":
            return self._table(element)
        if tag == "hr":
            return ["---"]
        return self.blocks(element, depth=depth)

    def _list_item(
        self, item: lxml_html.HtmlElement, marker: str, *, depth: int
    ) -> str:
        content = "\n".join(self.blocks(item, depth=depth + 1))
        if not content or depth >= _MAX_NESTING:
            # Past the indented levels the lines are left as they are, so a
            # deep list costs one pass over its text rather than one per level.
            return marker + content if content else ""
        first, *rest = content.split("\n")
        indent = " " * len(marker)
        return "\n".join(
            [marker + first, *(indent + line if line else "" for line in rest)]
        )

    def _preformatted(self, element: lxml_html.HtmlElement) -> str:
        """Code as laid out, where highlighters put each line in its own block."""

        parts = [element.text or ""]
        for child in element:
            if child.tag == "br":
                parts.append("\n")
            elif self._renders(child):
                inner = self._preformatted(child)
                ends_line = child.tag in _BLOCKS and not inner.endswith("\n")
                parts.append(inner + "\n" if ends_line else inner)
            parts.append(child.tail or "")
        return "".join(parts)

    def _table(self, table: lxml_html.HtmlElement) -> list[str]:
        rows = [
            [
                _collapse(self.inline(cell)).strip()
                for cell in row
                if cell.tag in {"td", "th"} and self._renders(cell)
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

    def inline(self, element: lxml_html.HtmlElement) -> str:
        tag = element.tag
        if tag == "br":
            return "\n"
        text = _collapse(element.text) + "".join(
            (self.inline(child) if self._renders(child) else "") + _collapse(child.tail)
            for child in element
        )
        if tag == "code":
            return f"`{text.strip()}`" if text.strip() else ""
        if tag == "a":
            label = text.strip()
            href = (element.get("href") or "").strip()
            scheme = _HREF_SCHEME.match(href)
            # An anchor on the same page, or a javascript:/mailto: target, is
            # no page to read next.
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
