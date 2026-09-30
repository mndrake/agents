"""Builders for the HTML Help "#" system files stored inside a CHM.

* ``#SYSTEM``  - project options (title, default topic, contents/index files...)
* ``#STRINGS`` - string pool referenced by the other files
* ``#WINDOWS`` - window definitions (HH_WINTYPE records)
* ``#TOPICS``, ``#URLTBL``, ``#URLSTR`` - the topic table
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

# HH_WINTYPE fsValidMembers
HHWIN_PARAM_PROPERTIES = 1 << 1
HHWIN_PARAM_STYLES = 1 << 2
HHWIN_PARAM_EXSTYLES = 1 << 3
HHWIN_PARAM_RECT = 1 << 4
HHWIN_PARAM_NAV_WIDTH = 1 << 5
HHWIN_PARAM_SHOWSTATE = 1 << 6
HHWIN_PARAM_TB_FLAGS = 1 << 8
HHWIN_PARAM_EXPANSION = 1 << 9
HHWIN_PARAM_TABPOS = 1 << 10
HHWIN_PARAM_CUR_TAB = 1 << 13

# fsWinProperties
HHWIN_PROP_TRI_PANE = 1 << 5
HHWIN_PROP_AUTO_SYNC = 1 << 8
HHWIN_PROP_TAB_SEARCH = 1 << 10
HHWIN_PROP_CHANGE_TITLE = 1 << 13
HHWIN_PROP_USER_POS = 1 << 18

# fsToolBarFlags
HHWIN_BUTTON_EXPAND = 1 << 1
HHWIN_BUTTON_BACK = 1 << 2
HHWIN_BUTTON_FORWARD = 1 << 3
HHWIN_BUTTON_STOP = 1 << 4
HHWIN_BUTTON_REFRESH = 1 << 5
HHWIN_BUTTON_HOME = 1 << 6
HHWIN_BUTTON_OPTIONS = 1 << 12
HHWIN_BUTTON_PRINT = 1 << 13

DEFAULT_NAV_PROPS = HHWIN_PROP_TRI_PANE | HHWIN_PROP_AUTO_SYNC | HHWIN_PROP_CHANGE_TITLE | \
    HHWIN_PROP_USER_POS
DEFAULT_BUTTONS = HHWIN_BUTTON_EXPAND | HHWIN_BUTTON_BACK | HHWIN_BUTTON_FORWARD | \
    HHWIN_BUTTON_STOP | HHWIN_BUTTON_REFRESH | HHWIN_BUTTON_HOME | HHWIN_BUTTON_OPTIONS | \
    HHWIN_BUTTON_PRINT

WS_DEFAULT = 0  # let the viewer pick


@dataclass
class WindowDef:
    """A [WINDOWS] entry of an .hhp project."""
    name: str
    caption: str = ""
    toc: str = ""
    index: str = ""
    default_file: str = ""
    home: str = ""
    jump1_url: str = ""
    jump1_text: str = ""
    jump2_url: str = ""
    jump2_text: str = ""
    nav_props: Optional[int] = None
    nav_width: Optional[int] = None
    buttons: Optional[int] = None
    rect: Optional[List[int]] = None
    styles: Optional[int] = None
    ex_styles: Optional[int] = None
    show_state: Optional[int] = None
    not_expanded: Optional[int] = None
    nav_tab: Optional[int] = None
    tab_pos: Optional[int] = None
    notify_id: Optional[int] = None


class StringTable:
    """#STRINGS: NUL-terminated strings; offset 0 is the empty string."""

    BLOCK = 0x1000

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self.data = bytearray(b"\0")
        self.index: Dict[str, int] = {"": 0}

    def add(self, s: Optional[str]) -> int:
        if not s:
            return 0
        if s in self.index:
            return self.index[s]
        raw = s.encode(self.encoding, errors="replace") + b"\0"
        # keep strings from straddling 4 KiB blocks, like HHC does
        room = self.BLOCK - (len(self.data) % self.BLOCK)
        if len(raw) > room and len(raw) <= self.BLOCK:
            self.data += bytes(room)
        off = len(self.data)
        self.data += raw
        self.index[s] = off
        return off


def _filetime(ts: float) -> int:
    return int((ts + 11644473600) * 10_000_000)


def _sys_entry(code: int, data: bytes) -> bytes:
    return struct.pack("<HH", code, len(data)) + data


def _sz(s: str, enc: str) -> bytes:
    return s.encode(enc, errors="replace") + b"\0"


def build_system(*, title: str, default_topic: str, contents_file: str, index_file: str,
                 default_window: str, compiled_name: str, lcid: int, encoding: str,
                 default_font: str = "", full_text_search: bool = False,
                 binary_toc_key: Optional[int] = None, idxhdr: bytes = b"",
                 timestamp: Optional[float] = None) -> bytes:
    ts = time.time() if timestamp is None else timestamp
    out = struct.pack("<I", 3)  # version
    out += _sys_entry(10, struct.pack("<I", int(ts) & 0xFFFFFFFF))
    out += _sys_entry(9, _sz("HHA Version 4.74.8702", encoding))
    ft = _filetime(ts)
    out += _sys_entry(4, struct.pack("<IIIIIQII", lcid, 0, int(full_text_search), 0, 0,
                                     ft, 0, 0))
    # With a binary TOC hhc.exe leaves out the contents file (code 0), so the
    # viewer reads #TOCIDX.
    if contents_file and binary_toc_key is None:
        out += _sys_entry(0, _sz(contents_file, encoding))
    if index_file:
        out += _sys_entry(1, _sz(index_file, encoding))
    if default_topic:
        out += _sys_entry(2, _sz(default_topic, encoding))
    out += _sys_entry(3, _sz(title, encoding))
    if default_window:
        out += _sys_entry(5, _sz(default_window, encoding))
    out += _sys_entry(6, _sz(compiled_name, encoding))
    if default_font:
        out += _sys_entry(16, _sz(default_font, encoding))
    if binary_toc_key is not None:  # #URLTBL key of the contents file's URL
        out += _sys_entry(11, struct.pack("<I", binary_toc_key))
    out += _sys_entry(12, struct.pack("<I", 0))  # number of information types
    if idxhdr:
        out += _sys_entry(13, idxhdr)
        out += _sys_entry(15, struct.pack("<I", 0))  # information type checksum
    return out


def build_windows(windows: List[WindowDef], strings: StringTable) -> bytes:
    out = struct.pack("<II", len(windows), 196)
    for w in windows:
        valid = 0
        props = w.nav_props if w.nav_props is not None else DEFAULT_NAV_PROPS
        valid |= HHWIN_PARAM_PROPERTIES
        styles = w.styles or 0
        if w.styles is not None:
            valid |= HHWIN_PARAM_STYLES
        ex_styles = w.ex_styles or 0
        if w.ex_styles is not None:
            valid |= HHWIN_PARAM_EXSTYLES
        rect = w.rect or [0, 0, 0, 0]
        if w.rect:
            valid |= HHWIN_PARAM_RECT
        show = w.show_state or 0
        if w.show_state is not None:
            valid |= HHWIN_PARAM_SHOWSTATE
        nav_width = w.nav_width if w.nav_width is not None else 250
        valid |= HHWIN_PARAM_NAV_WIDTH
        buttons = w.buttons if w.buttons is not None else DEFAULT_BUTTONS
        valid |= HHWIN_PARAM_TB_FLAGS
        not_expanded = w.not_expanded or 0
        if w.not_expanded is not None:
            valid |= HHWIN_PARAM_EXPANSION
        nav_tab = w.nav_tab or 0
        if w.nav_tab is not None:
            valid |= HHWIN_PARAM_CUR_TAB
        tab_pos = w.tab_pos or 0
        if w.tab_pos is not None:
            valid |= HHWIN_PARAM_TABPOS

        rec = struct.pack("<iIIII", 196, 0, strings.add(w.name), valid, props)
        rec += struct.pack("<III", strings.add(w.caption), styles, ex_styles)
        rec += struct.pack("<iiii", *rect)
        rec += struct.pack("<i", show)
        rec += struct.pack("<IIIIII", 0, 0, 0, 0, 0, 0)  # HWNDs / info types
        rec += struct.pack("<i", nav_width)
        rec += struct.pack("<iiii", 0, 0, 0, 0)  # rcHTML
        rec += struct.pack("<IIII", strings.add(w.toc), strings.add(w.index),
                           strings.add(w.default_file), strings.add(w.home))
        rec += struct.pack("<IIiii", buttons, not_expanded, nav_tab, tab_pos, w.notify_id or 0)
        rec += bytes(20)  # tabOrder
        rec += struct.pack("<i", 0)  # cHistory
        rec += struct.pack("<IIII", strings.add(w.jump1_text), strings.add(w.jump2_text),
                           strings.add(w.jump1_url), strings.add(w.jump2_url))
        rec += struct.pack("<iiii", 0, 0, 0, 0)  # rcMinSize
        rec += struct.pack("<iI", 0, 0)  # cbInfoTypes, pszCustomTabs
        assert len(rec) == 196, len(rec)
        out += rec
    return out


@dataclass
class TopicTables:
    topics: bytes = b""
    urltbl: bytes = b""
    urlstr: bytes = b""


@dataclass
class Topic:
    """A #TOPICS entry. ``url`` is the path inside the CHM, without a leading
    slash (``html/page.htm``), as hhc.exe stores it."""
    url: str
    title: Optional[str] = None
    toc_offset: int = 0       # node in #TOCIDX, 0 if none
    in_contents: bool = False


def url_hash(url: str) -> int:
    """The key #URLTBL is sorted on; the viewer binary-searches it to map a
    URL to its topic. Each character is a base-43 digit counted from '0',
    with characters above 'Z' folded down by 32 and '/' counted as '\\'.
    Reproduces all 6,697 keys of the Excel 2013 developer documentation."""
    h = 0
    for c in url.encode("utf-8"):
        if c in (0x2F, 0x5C):
            c = 0x5C
        elif c > 0x5A:
            c = (c - 32) & 0xFF
        h = (h * 43 + c - 0x30) & 0xFFFFFFFF
    return h


def build_topics(topics: List[Topic], strings: StringTable) -> TopicTables:
    urlstr = bytearray()

    def add_urlstr(url: str) -> int:
        raw = url.encode("utf-8") + b"\0"
        room = 0x4000 - (len(urlstr) % 0x4000)
        if room < 8 + len(raw):
            urlstr.extend(bytes(room))
        if len(urlstr) % 0x4000 == 0:
            urlstr.append(0)
        off = len(urlstr)
        urlstr.extend(struct.pack("<II", 0, 0) + raw)
        return off

    str_offs = [add_urlstr(t.url) for t in topics]
    # #URLTBL: 341 12-byte rows per 4 KiB block, each block ending in DWORD
    # 4096, rows sorted by URL hash.
    order = sorted(range(len(topics)), key=lambda i: url_hash(topics[i].url))
    urltbl = bytearray()
    url_offs = [0] * len(topics)
    for i in order:
        if (len(urltbl) & 0xFFF) == 0xFFC:
            urltbl.extend(struct.pack("<I", 0x1000))
        url_offs[i] = len(urltbl)
        urltbl.extend(struct.pack("<III", url_hash(topics[i].url), i, str_offs[i]))

    tops = bytearray()
    for i, t in enumerate(topics):
        title_off = strings.add(t.title) if t.title else 0xFFFFFFFF
        tops.extend(struct.pack("<IIIHH", t.toc_offset, title_off, url_offs[i],
                                6 if t.in_contents else 2, 0))
    return TopicTables(bytes(tops), bytes(urltbl), bytes(urlstr))


def build_idxhdr(topic_count: int, timestamp: int, window_styles: Optional[int] = None,
                 ex_window_styles: Optional[int] = None) -> bytes:
    """#IDXHDR (also stored as #SYSTEM code 13) for a binary TOC, with the
    values hhc.exe writes when the contents file sets no site properties."""
    none = 0xFFFFFFFF
    hdr = struct.pack("<4sIIIIIIIIIIIIIIIIIII", b"T#SM", timestamp & 0xFFFFFFFF, 1,
                      topic_count, 0, none, 0, 0, none, none, none,
                      none if window_styles is None else window_styles,
                      none if ex_window_styles is None else ex_window_styles,
                      none, none, none, 0, 1, 0, 0)
    return hdr + bytes(0x1000 - len(hdr))
