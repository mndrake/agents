"""Read and write HTML Help sitemap files (.hhc table of contents, .hhk index)."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import List, Optional


@dataclass
class SitemapItem:
    name: str
    local: str = ""
    children: List["SitemapItem"] = field(default_factory=list)
    image_number: Optional[int] = None


class _SitemapParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: List[SitemapItem] = []
        self.stack: List[List[SitemapItem]] = [self.root]
        self.current: Optional[dict] = None
        self.depth_started = False
        self.properties: dict = {}
        self.in_properties = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "ul":
            if self.depth_started:
                # A nested <UL> holds the children of the last item at the
                # current level. Some compilers (e.g. the Office 2013 docs)
                # wrap each child in its own <UL>, so this must not be the
                # last item parsed, which may sit deeper in a closed sibling.
                level = self.stack[-1]
                self.stack.append(level[-1].children if level else level)
            self.depth_started = True
        elif tag == "object" and a.get("type", "").lower() == "text/sitemap":
            self.current = {}
        elif tag == "object" and a.get("type", "").lower() == "text/site properties":
            self.in_properties = True
        elif tag == "param" and self.in_properties:
            self.properties[a.get("name", "").lower()] = a.get("value", "")
        elif tag == "param" and self.current is not None:
            name = a.get("name", "").lower()
            # Keep the first value; "Name"/"Local" may repeat for multi-topic keywords.
            self.current.setdefault(name, a.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "ul":
            if len(self.stack) > 1:
                self.stack.pop()
        elif tag == "object" and self.in_properties:
            self.in_properties = False
        elif tag == "object" and self.current is not None:
            c = self.current
            self.current = None
            item = SitemapItem(c.get("name", ""), c.get("local", ""))
            if c.get("imagenumber", "").isdigit():
                item.image_number = int(c["imagenumber"])
            self.stack[-1].append(item)


def parse_sitemap(text: str) -> List[SitemapItem]:
    p = _SitemapParser()
    p.feed(text)
    p.close()
    return p.root


def site_properties(text: str) -> dict:
    """Params of the "text/site properties" object, keyed by lowercase name."""
    p = _SitemapParser()
    p.feed(text)
    p.close()
    return p.properties


def iter_locals(items: List[SitemapItem]):
    for it in items:
        if it.local:
            yield it.local
        yield from iter_locals(it.children)


def _write_items(items: List[SitemapItem], indent: int, out: List[str]) -> None:
    pad = "\t" * indent
    out.append(f"{pad}<UL>")
    for it in items:
        out.append(f'{pad}\t<LI> <OBJECT type="text/sitemap">')
        out.append(f'{pad}\t\t<param name="Name" value="{html.escape(it.name, quote=True)}">')
        if it.local:
            out.append(f'{pad}\t\t<param name="Local" value="{html.escape(it.local, quote=True)}">')
        if it.image_number is not None:
            out.append(f'{pad}\t\t<param name="ImageNumber" value="{it.image_number}">')
        out.append(f"{pad}\t\t</OBJECT>")
        if it.children:
            _write_items(it.children, indent + 1, out)
    out.append(f"{pad}</UL>")


def write_sitemap(items: List[SitemapItem], *, is_index: bool = False) -> str:
    out = [
        '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML//EN">',
        "<HTML>",
        "<HEAD>",
        '<meta name="GENERATOR" content="chmc">',
        "<!-- Sitemap 1.0 -->",
        "</HEAD><BODY>",
    ]
    if not is_index:
        out.append('<OBJECT type="text/site properties">')
        out.append('\t<param name="Window Styles" value="0x800025">')
        out.append("</OBJECT>")
    else:
        out.append('<OBJECT type="text/site properties">')
        out.append("</OBJECT>")
    _write_items(items, 0, out)
    out.append("</BODY></HTML>")
    return "\n".join(out) + "\n"
