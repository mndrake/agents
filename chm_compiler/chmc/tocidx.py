"""Binary table of contents (``#TOCIDX``) writer and reader.

Layout, as written by hhc.exe (checked against the Excel 2013 developer
documentation, whose 6,694-entry tree decodes to exactly its .hhc)::

    0x0000  header: DWORD 0x1000, DWORD offset of the records,
            DWORD record count, DWORD offset of the topic list; zero filled
    0x1000  one node per TOC entry, level by level (all top-level entries,
            then all their children, ...); a node never crosses a 4 KiB
            boundary, the gap before the boundary is zero filled
    ...     topic list: the #TOPICS index of every entry with a Local, in
            document (pre-)order
    ...     16-byte records, one per entry with a Local, in the same order:
            node offset, sequence number from 666, offset of the entry's
            slot in the topic list, number of topic-list slots in its subtree

A node is ``WORD 0, WORD record index, DWORD flags, DWORD topic index (or,
without a Local, the #STRINGS offset of the name), DWORD parent node, DWORD
next sibling``; books (flag 0x4) add ``DWORD first child, DWORD 0``.
hhc.exe stops filling the records after a few hundred and leaves stale memory
in the rest (with flag 0x40 on the books concerned); chmc writes them all.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .sitemap import SitemapItem

BLOCK = 0x1000
SEQ_START = 666

FLAG_NEW = 0x2
FLAG_BOOK = 0x4
FLAG_LOCAL = 0x8
FLAG_BOOK_EXTRA = 0x1 | 0x100  # always set on books by hhc.exe


@dataclass
class _Node:
    item: SitemapItem
    parent: Optional["_Node"]
    offset: int = 0
    children: List["_Node"] = field(default_factory=list)
    topic: Optional[int] = None
    record: int = 0

    @property
    def is_book(self) -> bool:
        return bool(self.children)

    @property
    def size(self) -> int:
        return 28 if self.is_book else 20


def build_tocidx(items: List[SitemapItem],
                 topic_for: Callable[[SitemapItem, int], Optional[int]],
                 string_for: Callable[[str], int]) -> bytes:
    """Serialise ``items``.

    ``topic_for(item, node_offset)`` returns the #TOPICS index for an entry
    with a Local (and may record the node offset for that topic);
    ``string_for(name)`` returns the #STRINGS offset of a name.
    """
    def make(its: List[SitemapItem], parent: Optional[_Node]) -> List[_Node]:
        out = []
        for it in its:
            n = _Node(it, parent)
            n.children = make(it.children, n)
            out.append(n)
        return out

    roots = make(items, None)
    if not roots:
        return b""

    # Level order, packing nodes into 4 KiB blocks.
    order: List[_Node] = []
    level = roots
    pos = BLOCK
    while level:
        nxt = []
        for n in level:
            if pos // BLOCK != (pos + n.size - 1) // BLOCK:
                pos = (pos // BLOCK + 1) * BLOCK
            n.offset = pos
            pos += n.size
            order.append(n)
            nxt.extend(n.children)
        level = nxt
    nodes_end = pos

    # Document order: topics, records and subtree sizes.
    preorder: List[_Node] = []

    def walk(ns: List[_Node]) -> None:
        for n in ns:
            preorder.append(n)
            walk(n.children)
    walk(roots)

    records: List[_Node] = []
    for n in preorder:
        n.record = len(records)
        if n.item.local:
            n.topic = topic_for(n.item, n.offset)
            if n.topic is not None:
                records.append(n)

    slots = {}
    for n in reversed(preorder):  # children before parents
        slots[id(n)] = (n.topic is not None) + sum(slots[id(c)] for c in n.children)

    topic_list_off = nodes_end
    records_off = topic_list_off + 4 * len(records)
    out = bytearray(records_off + 16 * len(records))
    struct.pack_into("<IIII", out, 0, BLOCK, records_off, len(records), topic_list_off)

    next_off = {}
    for group in [roots] + [n.children for n in order]:
        for a, b in zip(group, group[1:]):
            next_off[id(a)] = b.offset

    for n in order:
        flags = FLAG_LOCAL if n.topic is not None else 0
        if n.is_book:
            flags |= FLAG_BOOK | FLAG_BOOK_EXTRA
        ref = n.topic if n.topic is not None else string_for(n.item.name)
        nxt = next_off.get(id(n), 0)
        struct.pack_into("<HHIIII", out, n.offset, 0, n.record & 0xFFFF, flags, ref,
                         n.parent.offset if n.parent else 0, nxt)
        if n.is_book:
            struct.pack_into("<II", out, n.offset + 20, n.children[0].offset, 0)

    for i, n in enumerate(records):
        struct.pack_into("<I", out, topic_list_off + 4 * i, n.topic)
        struct.pack_into("<IIII", out, records_off + 16 * i, n.offset, SEQ_START + i,
                         topic_list_off + 4 * i, slots[id(n)])
    return bytes(out)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

@dataclass
class TocNode:
    offset: int
    flags: int
    ref: int                 # topic index (flag 0x8) or #STRINGS offset
    record: int
    parent: int
    next: int
    child: int = 0
    children: List["TocNode"] = field(default_factory=list)

    @property
    def has_local(self) -> bool:
        return bool(self.flags & FLAG_LOCAL)


def read_tocidx(data: bytes) -> List[TocNode]:
    """Decode the node tree (top-level nodes with their children)."""
    if len(data) < BLOCK:
        raise ValueError("#TOCIDX shorter than its header")
    seen = set()

    def chain(off: int, parent: int) -> List[TocNode]:
        out = []
        while off:
            if off in seen or off + 20 > len(data):
                raise ValueError(f"bad #TOCIDX node offset {off:#x}")
            seen.add(off)
            _w0, rec, flags, ref, par, nxt = struct.unpack_from("<HHIIII", data, off)
            if par != parent:
                raise ValueError(f"node {off:#x} has parent {par:#x}, expected {parent:#x}")
            n = TocNode(off, flags, ref, rec, par, nxt)
            if flags & FLAG_BOOK:
                n.child, = struct.unpack_from("<I", data, off + 20)
                n.children = chain(n.child, off)
            out.append(n)
            off = nxt
        return out

    return chain(BLOCK, 0)
