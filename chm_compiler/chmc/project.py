"""Project loading (.hhp files or plain folders) and compilation to .chm."""

from __future__ import annotations

import html
import os
import posixpath
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from . import internal, sitemap
from .itsf import ITSFWriter

HTML_EXTS = {".htm", ".html", ".xhtml", ".shtml"}
SKIP_EXTS = {".hhp", ".chm", ".chw", ".log"}

# LCID -> Windows ANSI code page used for strings in #SYSTEM/#STRINGS.
LCID_CODEPAGES = {
    0x0401: "cp1256", 0x0404: "cp950", 0x0405: "cp1250", 0x0408: "cp1253",
    0x040D: "cp1255", 0x040E: "cp1250", 0x0411: "cp932", 0x0412: "cp949",
    0x0415: "cp1250", 0x0419: "cp1251", 0x041B: "cp1250", 0x041E: "cp874",
    0x041F: "cp1254", 0x0422: "cp1251", 0x042A: "cp1258", 0x0804: "gbk",
    0x0C04: "cp950", 0x0424: "cp1250", 0x0425: "cp1257", 0x0426: "cp1257",
    0x0427: "cp1257", 0x0402: "cp1251",
}

Log = Callable[[str], None]


class ProjectError(Exception):
    pass


@dataclass
class Project:
    base_dir: str
    title: str = ""
    compiled_file: str = ""
    contents_file: str = ""
    index_file: str = ""
    default_topic: str = ""
    default_window: str = ""
    default_font: str = ""
    lcid: int = 0x0409
    files: List[str] = field(default_factory=list)  # paths relative to base_dir
    windows: List[internal.WindowDef] = field(default_factory=list)
    generated: Dict[str, bytes] = field(default_factory=dict)  # virtual files
    follow_links: bool = True
    source: str = ""

    @property
    def encoding(self) -> str:
        return LCID_CODEPAGES.get(self.lcid, "cp1252")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def norm_rel(path: str) -> str:
    """Normalise a project-relative path to the forward-slash form used in CHMs."""
    path = path.replace("\\", "/").strip()
    path = posixpath.normpath(path)
    if path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _read_text(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _parse_int(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _split_csv(line: str) -> List[str]:
    """Split a [WINDOWS] value: comma separated, strings in double quotes."""
    out, cur, in_q, bracket = [], "", False, 0
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == "[" and not in_q:
            bracket += 1
            cur += ch
        elif ch == "]" and not in_q:
            bracket -= 1
            cur += ch
        elif ch == "," and not in_q and bracket == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def parse_window(name: str, value: str) -> internal.WindowDef:
    f = _split_csv(value) + [""] * 20
    w = internal.WindowDef(name=name, caption=f[0], toc=norm_rel(f[1]) if f[1] else "",
                           index=norm_rel(f[2]) if f[2] else "",
                           default_file=norm_rel(f[3]) if f[3] else "",
                           home=norm_rel(f[4]) if f[4] else "",
                           jump1_url=f[5], jump1_text=f[6], jump2_url=f[7], jump2_text=f[8])
    w.nav_props = _parse_int(f[9])
    w.nav_width = _parse_int(f[10])
    w.buttons = _parse_int(f[11])
    rect = f[12].strip("[] ")
    if rect:
        nums = [_parse_int(x) for x in rect.split(",")]
        if len(nums) == 4 and all(n is not None for n in nums):
            w.rect = nums  # type: ignore[assignment]
    w.styles = _parse_int(f[13])
    w.ex_styles = _parse_int(f[14])
    w.show_state = _parse_int(f[15])
    w.not_expanded = _parse_int(f[16])
    w.nav_tab = _parse_int(f[17])
    w.tab_pos = _parse_int(f[18])
    w.notify_id = _parse_int(f[19])
    return w


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_hhp(path: str) -> Project:
    """Load a Microsoft HTML Help Workshop project file."""
    text = _read_text(path)
    proj = Project(base_dir=os.path.dirname(os.path.abspath(path)), source=path)
    section = None
    options: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = m.group(1).strip().upper()
            continue
        if section == "OPTIONS" and "=" in line:
            k, v = line.split("=", 1)
            options[k.strip().lower()] = v.strip()
        elif section == "FILES":
            proj.files.append(norm_rel(line))
        elif section == "WINDOWS" and "=" in line:
            k, v = line.split("=", 1)
            proj.windows.append(parse_window(k.strip(), v))

    proj.title = options.get("title", "")
    proj.contents_file = norm_rel(options["contents file"]) if options.get("contents file") else ""
    proj.index_file = norm_rel(options["index file"]) if options.get("index file") else ""
    proj.default_topic = norm_rel(options["default topic"]) if options.get("default topic") else ""
    proj.default_window = options.get("default window", "")
    proj.default_font = options.get("default font", "")
    compiled = options.get("compiled file", "")
    proj.compiled_file = compiled.replace("\\", "/") if compiled else \
        os.path.splitext(os.path.basename(path))[0] + ".chm"
    lang = options.get("language", "")
    if lang:
        lcid = _parse_int(lang.split()[0])
        if lcid:
            proj.lcid = lcid
    return proj


def _html_title(path: str) -> str:
    try:
        text = _read_text(path)
    except OSError:
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not m:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.I | re.S)
    if not m:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).split())


def load_folder(folder: str, title: str = "", default_topic: str = "",
                make_toc: bool = True, make_index: bool = True) -> Project:
    """Build a project from every file in ``folder``.

    A table of contents mirroring the folder tree (and a keyword index of page
    titles) is generated unless the folder already has ``.hhc``/``.hhk`` files.
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ProjectError(f"not a folder: {folder}")
    proj = Project(base_dir=folder, source=folder, follow_links=False)
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "#", "$")))
        for fn in sorted(files):
            if fn.startswith((".", "#", "$")):  # hidden files, decompiled CHM internals
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in SKIP_EXTS:
                continue
            rel = norm_rel(os.path.relpath(os.path.join(root, fn), folder))
            proj.files.append(rel)
            if ext == ".hhc" and not proj.contents_file:
                proj.contents_file = rel
            elif ext == ".hhk" and not proj.index_file:
                proj.index_file = rel

    html_files = [f for f in proj.files if os.path.splitext(f)[1].lower() in HTML_EXTS]
    if not html_files:
        raise ProjectError(f"no HTML files found in {folder}")

    if default_topic:
        proj.default_topic = norm_rel(default_topic)
    else:
        for cand in ("index.html", "index.htm", "default.html", "default.htm"):
            if cand in proj.files:
                proj.default_topic = cand
                break
        else:
            proj.default_topic = html_files[0]

    proj.title = title or _html_title(os.path.join(folder, proj.default_topic)) or \
        os.path.basename(folder)
    proj.compiled_file = os.path.basename(folder) + ".chm"

    titles = {f: _html_title(os.path.join(folder, f)) or
              os.path.splitext(posixpath.basename(f))[0] for f in html_files}

    if make_toc and not proj.contents_file:
        proj.contents_file = "toc.hhc"
        proj.generated["toc.hhc"] = sitemap.write_sitemap(
            _folder_toc(html_files, titles, proj.default_topic)).encode("utf-8")
    if make_index and not proj.index_file:
        proj.index_file = "index.hhk"
        entries = sorted(((t, f) for f, t in titles.items()), key=lambda x: x[0].lower())
        proj.generated["index.hhk"] = sitemap.write_sitemap(
            [sitemap.SitemapItem(t, f) for t, f in entries], is_index=True).encode("utf-8")

    proj.default_window = "main"
    proj.windows.append(internal.WindowDef(
        name="main", caption=proj.title, toc=proj.contents_file, index=proj.index_file,
        default_file=proj.default_topic, home=proj.default_topic))
    return proj


def _folder_toc(html_files: List[str], titles: Dict[str, str], default_topic: str):
    root: List[sitemap.SitemapItem] = []
    folders: Dict[str, sitemap.SitemapItem] = {}

    def folder_node(path: str) -> List[sitemap.SitemapItem]:
        if not path:
            return root
        if path not in folders:
            parent = folder_node(posixpath.dirname(path))
            node = sitemap.SitemapItem(posixpath.basename(path).replace("_", " ").title())
            folders[path] = node
            parent.append(node)
        return folders[path].children

    ordered = sorted(html_files, key=lambda f: (f != default_topic, f.count("/"), f.lower()))
    for f in ordered:
        d = posixpath.dirname(f)
        name = posixpath.basename(f).lower()
        # A folder's index page becomes the folder node's own link.
        if d and os.path.splitext(name)[0] in ("index", "default") and d in folders \
                and not folders[d].local:
            folders[d].local = f
            folders[d].name = titles[f] or folders[d].name
            continue
        if d and d not in folders and os.path.splitext(name)[0] in ("index", "default"):
            folder_node(d)
            folders[d].local = f
            folders[d].name = titles[f] or folders[d].name
            continue
        folder_node(d).append(sitemap.SitemapItem(titles[f], f))
    return root


def load_project(path: str) -> Project:
    if os.path.isdir(path):
        return load_folder(path)
    if path.lower().endswith(".hhp"):
        return load_hhp(path)
    raise ProjectError(f"expected a .hhp project file or a folder: {path}")


# ---------------------------------------------------------------------------
# Link discovery
# ---------------------------------------------------------------------------

class _LinkParser(HTMLParser):
    ATTRS = {"href", "src", "background", "data", "codebase", "longdesc", "poster"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[str] = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if v and k.lower() in self.ATTRS:
                self.links.append(v)
            elif v and k.lower() == "style":
                self.links.extend(_css_links(v))
            elif v and k.lower() == "srcset":
                self.links.extend(p.strip().split()[0] for p in v.split(",") if p.strip())
        if tag == "param":
            d = {k.lower(): v for k, v in attrs}
            if (d.get("name") or "").lower() in ("local", "url", "imagelist") and d.get("value"):
                self.links.append(d["value"])
        self.in_style = tag == "style"

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            self.links.extend(_css_links(data))


_CSS_URL = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)|@import\s+['"]([^'"]+)['"]""", re.I)


def _css_links(text: str) -> List[str]:
    return [a or b for a, b in _CSS_URL.findall(text)]


def _local_target(link: str, from_file: str) -> Optional[str]:
    link = link.strip()
    if not link or link.startswith("#"):
        return None
    parts = urlsplit(link)
    if parts.scheme or parts.netloc or link.startswith("//"):
        return None  # external, mk:@MSITStore:, javascript:, mailto: ...
    target = unquote(parts.path)
    if not target:
        return None
    if target.startswith("/"):
        return norm_rel(target)
    return norm_rel(posixpath.join(posixpath.dirname(from_file), target))


def discover_linked(base_dir: str, start: List[str]) -> List[str]:
    """Follow links from HTML/CSS files to collect local dependencies."""
    seen = set(start)
    queue = list(start)
    found: List[str] = []
    while queue:
        rel = queue.pop()
        full = os.path.join(base_dir, rel)
        ext = os.path.splitext(rel)[1].lower()
        if ext not in HTML_EXTS and ext not in (".css", ".hhc", ".hhk"):
            continue
        try:
            text = _read_text(full)
        except OSError:
            continue
        if ext == ".css":
            links = _css_links(text)
        else:
            p = _LinkParser()
            try:
                p.feed(text)
                p.close()
            except Exception:  # malformed HTML: take what we got
                pass
            links = p.links
        for link in links:
            target = _local_target(link, rel)
            if not target or target in seen or target.startswith(".."):
                continue
            if os.path.isfile(os.path.join(base_dir, target)):
                seen.add(target)
                found.append(target)
                queue.append(target)
    return found


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

@dataclass
class CompileResult:
    output: str
    size: int
    file_count: int
    uncompressed_size: int
    seconds: float
    warnings: List[str]


def compile_project(proj: Project, output: Optional[str] = None, level: int = 6,
                    log: Log = print) -> CompileResult:
    t0 = time.time()
    warnings: List[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        log("Warning: " + msg)

    if output is None:
        output = os.path.join(proj.base_dir, proj.compiled_file or "output.chm")
    output = os.path.abspath(output)
    log(f"Compiling {proj.source or proj.base_dir}")

    # Collect file list: explicit files, TOC/index, default topic, window files, linked files.
    wanted: List[str] = []
    seen = set()

    def want(rel: str) -> None:
        rel = norm_rel(rel)
        if rel and rel not in seen:
            seen.add(rel)
            wanted.append(rel)

    for f in proj.files:
        want(f)
    for f in (proj.contents_file, proj.index_file, proj.default_topic):
        if f:
            want(f)
    for w in proj.windows:
        for f in (w.toc, w.index, w.default_file, w.home):
            if f:
                want(f)

    toc_items: List[sitemap.SitemapItem] = []
    for sm in (proj.contents_file, proj.index_file):
        if not sm:
            continue
        data = proj.generated.get(sm)
        if data is None:
            full = os.path.join(proj.base_dir, sm)
            if not os.path.isfile(full):
                continue
            text = _read_text(full)
        else:
            text = data.decode("utf-8")
        items = sitemap.parse_sitemap(text)
        if sm == proj.contents_file:
            toc_items = items
        for local in sitemap.iter_locals(items):
            target = _local_target(local, "")
            if target:
                want(target)

    if proj.follow_links:
        existing = [f for f in wanted if f in proj.generated or
                    os.path.isfile(os.path.join(proj.base_dir, f))]
        for extra in discover_linked(proj.base_dir, existing):
            want(extra)

    out_rel = os.path.relpath(output, proj.base_dir).replace("\\", "/")
    writer = ITSFWriter(lcid=proj.lcid, compression_level=level)
    enc = proj.encoding
    strings = internal.StringTable(enc)

    included: List[str] = []
    total = 0
    file_data: Dict[str, bytes] = {}
    for rel in wanted:
        if rel == out_rel:
            continue
        if rel in proj.generated:
            data = proj.generated[rel]
        else:
            full = os.path.join(proj.base_dir, rel)
            if not os.path.isfile(full):
                warn(f"file not found: {rel}")
                continue
            with open(full, "rb") as fh:
                data = fh.read()
        file_data[rel] = data
        included.append(rel)
        total += len(data)

    if proj.default_topic and proj.default_topic not in file_data:
        warn(f"default topic not included: {proj.default_topic}")

    # Topic table
    toc_locals = {_local_target(x, "") for x in sitemap.iter_locals(toc_items)}
    topics = []
    for rel in included:
        if os.path.splitext(rel)[1].lower() in HTML_EXTS:
            title = "" if rel in proj.generated else _html_title(os.path.join(proj.base_dir, rel))
            topics.append(("/" + rel, title, rel in toc_locals))
    tt = internal.build_topics([(p, t) for p, t, _ in topics], strings,
                               {p for p, _, inside in topics if inside})

    windows = list(proj.windows)
    windows_ok = {w.name for w in windows}
    default_window = proj.default_window
    if default_window and default_window not in windows_ok:
        warn(f"default window '{default_window}' is not defined")
        default_window = ""
    if not windows:
        windows.append(internal.WindowDef(
            name="main", caption=proj.title, toc=proj.contents_file, index=proj.index_file,
            default_file=proj.default_topic, home=proj.default_topic))
        default_window = "main"
    elif not default_window:
        default_window = windows[0].name
    windows_data = internal.build_windows(windows, strings)

    compiled_name = os.path.splitext(os.path.basename(output))[0]
    system = internal.build_system(
        title=proj.title, default_topic=proj.default_topic, contents_file=proj.contents_file,
        index_file=proj.index_file, default_window=default_window, compiled_name=compiled_name,
        lcid=proj.lcid, encoding=enc, default_font=proj.default_font)

    # Like HHC, keep #SYSTEM uncompressed in section 0 and add an empty #ITBITS.
    writer.add("/#ITBITS", b"", section=0)
    writer.add("/#SYSTEM", system, section=0)
    writer.add("/#WINDOWS", windows_data)
    writer.add("/#TOPICS", tt.topics)
    writer.add("/#URLTBL", tt.urltbl)
    writer.add("/#URLSTR", tt.urlstr)
    writer.add("/#STRINGS", bytes(strings.data))
    for rel in included:
        writer.add("/" + rel, file_data[rel])
        log(f"  + {rel} ({len(file_data[rel]):,} bytes)")

    log(f"Compressing {total:,} bytes in {len(included)} files (level {level})...")
    blob = writer.build()
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "wb") as fh:
        fh.write(blob)
    dt = time.time() - t0
    ratio = (100.0 * len(blob) / total) if total else 100.0
    log(f"Created {output}: {len(blob):,} bytes ({ratio:.0f}% of original), "
        f"{len(included)} files, {dt:.2f}s")
    return CompileResult(output, len(blob), len(included), total, dt, warnings)
