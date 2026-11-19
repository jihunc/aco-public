"""Local documentation links from maintained Markdown tokens, never executable examples."""

import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token


class ExplicitAnchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.anchors.update(
            value for name, value in attrs if value and (name == "id" or tag == "a" and name == "name")
        )


def tokens(path: Path) -> list[Token]:
    return MarkdownIt("commonmark").parse(path.read_text())


def anchors(parsed: list[Token]) -> set[str]:
    result: set[str] = set()
    html = ExplicitAnchors()
    for index, token in enumerate(parsed):
        if token.type == "heading_open":
            inline = parsed[index + 1]
            text = "".join(t.content for t in inline.children or [] if t.type in {"text", "code_inline"})
            # Repository heading convention: Unicode letters/numbers, spaces and
            # existing hyphens/underscores; duplicate headings get numeric suffixes.
            base = "".join(
                c
                for c in text.lower()
                if c in {"-", "_", " "} or unicodedata.category(c)[0] in {"L", "N", "M"}
            ).replace(" ", "-")
            candidate, number = base, 0
            while candidate in result:
                number += 1
                candidate = f"{base}-{number}"
            result.add(candidate)
        for item in [token, *(token.children or [])]:
            if item.type in {"html_inline", "html_block"}:
                html.feed(item.content)
    return result | html.anchors


def check_markdown_links(root: Path, documents: list[Path]) -> list[str]:
    errors: list[str] = []
    cached: dict[Path, set[str]] = {}
    root = root.resolve()
    for path in documents:
        parsed = tokens(path)
        for token in parsed:
            for item in token.children or []:
                if item.type not in {"link_open", "image"}:
                    continue
                target = item.attrGet("href" if item.type == "link_open" else "src")
                if not isinstance(target, str) or not target:
                    continue
                url = urlsplit(target)
                if url.scheme or url.netloc:
                    continue
                destination = (path.parent / unquote(url.path)).resolve() if url.path else path.resolve()
                label = str(path.relative_to(root))
                if not destination.is_relative_to(root) or not destination.exists():
                    errors.append(f"Missing relative link in {label}: {target}")
                    continue
                if url.fragment and destination.suffix.lower() == ".md":
                    if destination not in cached:
                        cached[destination] = anchors(tokens(destination))
                    if unquote(url.fragment) not in cached[destination]:
                        errors.append(f"Missing Markdown link anchor in {label}: {target}")
    return errors
