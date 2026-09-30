"""``chmc init``: create a small, ready-to-build HTML Help project."""

from __future__ import annotations

import html
import os
import struct
import zlib

from . import sitemap

CSS = """body { font-family: Segoe UI, Tahoma, sans-serif; font-size: 10pt; margin: 16px; color: #222; }
h1 { font-size: 16pt; color: #1f4e79; border-bottom: 1px solid #ccd; padding-bottom: 4px; }
h2 { font-size: 12pt; color: #1f4e79; }
code, pre { font-family: Consolas, monospace; background: #f4f4f8; }
pre { padding: 8px; border: 1px solid #dde; }
a { color: #0563c1; }
"""

PAGES = [
    ("index.htm", "Welcome", """<p>Welcome to <b>{title}</b>.</p>
<p>This help file was compiled with <code>chmc</code>. Use the <b>Contents</b> tab to
browse topics, or the <b>Index</b> tab to find keywords.</p>
<ul>
  <li><a href="topics/getting_started.htm">Getting started</a></li>
  <li><a href="topics/faq.htm">Frequently asked questions</a></li>
</ul>"""),
    ("topics/getting_started.htm", "Getting Started", """<p>Edit the HTML files in this
folder, then rebuild:</p>
<pre>python -m chmc build {hhp}</pre>
<p>Images and stylesheets referenced from pages are picked up automatically.</p>
<p><img src="../images/logo.png" alt="logo" width="48" height="48"></p>"""),
    ("topics/faq.htm", "FAQ", """<h2>Where is the table of contents defined?</h2>
<p>In <code>toc.hhc</code>. The keyword index lives in <code>index.hhk</code>.</p>
<h2>How do I change the title?</h2>
<p>Edit <code>Title=</code> in the <code>[OPTIONS]</code> section of the .hhp file.</p>
<p><a href="../index.htm">Back to the welcome page</a></p>"""),
]

def _logo_png(size: int = 48) -> bytes:
    """A small PNG (blue rounded tile with a white "?"), built with zlib only."""
    glyph = ["01110", "10001", "00001", "00110", "00100", "00000", "00100"]
    scale, gx, gy = 4, (size - 5 * 4) // 2, (size - 7 * 4) // 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            corner = min(x, size - 1 - x) + min(y, size - 1 - y) < 6
            gxi, gyi = (x - gx) // scale, (y - gy) // scale
            on = 0 <= gxi < 5 and 0 <= gyi < 7 and x >= gx and y >= gy and glyph[gyi][gxi] == "1"
            row += bytes((255, 255, 255, 0) if corner else (255, 255, 255, 255) if on
                         else (31, 78, 121, 255))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + \
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + \
        chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")


def _page(title: str, body: str, css_rel: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{css_rel}">
</head>
<body>
<h1>{html.escape(title)}</h1>
{body}
</body>
</html>
"""


def create_project(folder: str, title: str = "My Help File") -> str:
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(os.path.abspath(folder)) or "help"
    hhp_name = name + ".hhp"
    for rel, page_title, body in PAGES:
        full = os.path.join(folder, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        css_rel = "../styles.css" if "/" in rel else "styles.css"
        with open(full, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(_page(page_title, body.format(title=html.escape(title), hhp=hhp_name), css_rel))
    os.makedirs(os.path.join(folder, "images"), exist_ok=True)
    with open(os.path.join(folder, "images", "logo.png"), "wb") as fh:
        fh.write(_logo_png())
    with open(os.path.join(folder, "styles.css"), "w", encoding="utf-8") as fh:
        fh.write(CSS)

    toc = [
        sitemap.SitemapItem("Welcome", "index.htm"),
        sitemap.SitemapItem("Topics", "", [
            sitemap.SitemapItem("Getting Started", "topics/getting_started.htm"),
            sitemap.SitemapItem("FAQ", "topics/faq.htm"),
        ]),
    ]
    index = [
        sitemap.SitemapItem("FAQ", "topics/faq.htm"),
        sitemap.SitemapItem("Getting started", "topics/getting_started.htm"),
        sitemap.SitemapItem("Table of contents", "topics/faq.htm"),
        sitemap.SitemapItem("Welcome", "index.htm"),
    ]
    with open(os.path.join(folder, "toc.hhc"), "w", encoding="utf-8") as fh:
        fh.write(sitemap.write_sitemap(toc))
    with open(os.path.join(folder, "index.hhk"), "w", encoding="utf-8") as fh:
        fh.write(sitemap.write_sitemap(index, is_index=True))

    hhp = os.path.join(folder, hhp_name)
    with open(hhp, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(f"""[OPTIONS]
Compatibility=1.1 or later
Compiled file={name}.chm
Contents file=toc.hhc
Index file=index.hhk
Default topic=index.htm
Default Window=main
Display compile progress=No
Full-text search=Yes
Language=0x409 English (United States)
Title={title}

[WINDOWS]
main="{title}","toc.hhc","index.hhk","index.htm","index.htm",,,,,0x42520,250,0x387e,[80,60,900,700],,,,,,,0

[FILES]
index.htm
topics/getting_started.htm
topics/faq.htm
""")
    return hhp
