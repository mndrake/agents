"""Full-text search index (``$FIftiMain``): word breaker, writer and reader.

The file is a B-tree of 4 KiB nodes keyed on lowercase words, preceded by a
0x400-byte header::

    header | WLC data, leaf | WLC data, leaf | ... | index nodes ... | root

Each leaf entry names a word (prefix-compressed against the previous entry
in the node), whether it is a title or body occurrence, and where its word
location codes (WLCs) are. A WLC block lists, per topic, the topic's index
in ``#TOPICS`` and the positions of the word within that topic, bit-packed
with the "scale and root" integer code. Index nodes hold the last word of
each child node; the tree is written bottom-up so the root is the last node.

Layouts follow the unofficial CHM specification (chmspec, "$FIftiMain") and
were checked against the index Microsoft's hhc.exe wrote for the Excel 2013
developer documentation.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Tuple

NODE_SIZE = 0x1000
HEADER_SIZE = 0x400
MAX_WORD = 99
SCALE = 2

# ---------------------------------------------------------------------------
# Integer codes
# ---------------------------------------------------------------------------


def encint_le(value: int) -> bytes:
    """Variable-length integer, 7 bits per byte, least significant group first."""
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decint_le(buf: bytes, pos: int) -> Tuple[int, int]:
    value = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, pos


def sr_bits(value: int, root: int) -> Tuple[int, int]:
    """Scale-and-root code (scale 2) for ``value``: returns (bits, bit count).

    A value that fits in ``root`` bits is ``0`` followed by those bits.
    Otherwise, with ``n`` significant bits, it is ``n - root`` ones, a zero
    and the low ``n - 1`` bits (the top bit is implied by the prefix).
    """
    n = value.bit_length()
    if n <= root:
        return value, root + 1
    ones = n - root
    q = n - 1
    prefix = ((1 << ones) - 1) << 1          # ones then a zero
    return (prefix << q) | (value & ((1 << q) - 1)), ones + 1 + q


class BitWriter:
    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def write(self, bits: int, count: int) -> None:
        self.acc = (self.acc << count) | bits
        self.n += count
        while self.n >= 8:
            self.n -= 8
            self.out.append((self.acc >> self.n) & 0xFF)
        self.acc &= (1 << self.n) - 1

    def align(self) -> None:
        if self.n:
            self.out.append((self.acc << (8 - self.n)) & 0xFF)
            self.acc = self.n = 0


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0  # in bits

    def bit(self) -> int:
        byte = self.data[self.pos >> 3]
        b = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def sr(self, root: int) -> int:
        ones = 0
        while self.bit():
            ones += 1
        if not ones:
            count, value = root, 0
        else:
            count, value = root + ones - 1, 1
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7


# ---------------------------------------------------------------------------
# Word breaker
# ---------------------------------------------------------------------------

# Latin-1 letters folded the way hhc.exe's word breaker folds them.
_FOLD = {}
for _chars, _to in (("àáâãäåÀÁÂÃÄÅ", "a"), ("æÆ", "ae"), ("çÇ", "c"), ("èéêëÈÉÊË", "e"),
                    ("ìíîïÌÍÎÏ", "i"), ("ðÐ", "d"), ("ñÑ", "n"), ("òóôõöøÒÓÔÕÖØ", "o"),
                    ("ùúûüÙÚÛÜ", "u"), ("ýÿÝŸ", "y"), ("ß", "ss"), ("šŠ", "s"),
                    ("œŒ", "oe"), ("¹", "1"), ("²", "2"), ("³", "3"), ("ª", "a"), ("º", "o")):
    for _c in _chars:
        _FOLD[ord(_c)] = _to

# Word rules of hhc.exe's breaker (ITIR StdWordBreaker), as observed in its
# output: letters, digits and "_" make words; an apostrophe inside a word is
# dropped ("isn't" -> "isnt"); a word that starts with a digit continues
# across runs of "." and "," followed by more word characters, keeping the
# periods and dropping the commas ("2,147,483,647" -> "2147483647",
# "3.14159" stays whole). Everything else separates words.
_WORD = re.compile(r"\d\w*(?:(?:[.,]+|['’])\w+)*|\w+(?:['’]\w+)*")


def fold_word(word: str) -> str:
    return word.lower().translate(_FOLD)


def split_words(text: str) -> List[str]:
    """Split text into lowercase index words."""
    out = []
    for w in _WORD.findall(text):
        w = w.replace("'", "").replace("’", "")
        if w[0].isdigit():
            w = w.replace(",", "")
        out.append(fold_word(w))
    return out


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "object", "applet", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[Tuple[int, str]] = []   # (context, text) in document order
        self.in_title = False
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag in self.SKIP:
            self.skip += 1
        self.parts.append((0, " "))

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        self.parts.append((0, " "))

    def handle_data(self, data):
        if self.in_title:
            self.parts.append((1, data))
        elif not self.skip:
            self.parts.append((0, data))


def page_words(html_text: str) -> List[Tuple[str, int]]:
    """Words of an HTML page in document order, as (word, context) pairs.

    Context 1 marks words of the <title>, 0 everything else. Like hhc.exe,
    title and body words share one position sequence.
    """
    p = _TextExtractor()
    try:
        p.feed(html_text)
        p.close()
    except Exception:  # malformed HTML: index what we got
        pass
    out: List[Tuple[str, int]] = []
    ctx, buf = 0, []
    for c, text in p.parts + [(-1, "")]:
        if c != ctx:
            out.extend((w, ctx) for w in split_words("".join(buf)))
            ctx, buf = c, []
        buf.append(text)
    return out


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

@dataclass
class FtsStats:
    words: int = 0          # distinct words
    occurrences: int = 0    # word occurrences
    topics: int = 0


class FtsIndex:
    """Collects word positions per topic and serialises ``$FIftiMain``."""

    def __init__(self, lcid: int = 0x0409, codepage: int = 1252) -> None:
        self.lcid = lcid
        self.codepage = codepage
        # (word, is_title) -> {topic: [positions]}
        self.hits: Dict[Tuple[bytes, int], Dict[int, List[int]]] = {}
        self.topics = 0
        self.total_words = 0
        self.total_length = 0
        self.encoding = "cp%d" % codepage

    def add_topic(self, topic: int, words: Iterable[Tuple[str, int]]) -> None:
        """Index one topic from its (word, context) pairs in document order."""
        self.topics = max(self.topics, topic + 1)
        for pos, (w, ctx) in enumerate(words):
            raw = w.encode(self.encoding, errors="ignore")
            if raw and len(raw) <= MAX_WORD:
                self.hits.setdefault((raw, ctx), {}).setdefault(topic, []).append(pos)
                self.total_words += 1
                self.total_length += len(raw)

    def add_html(self, topic: int, html_text: str) -> None:
        self.add_topic(topic, page_words(html_text))

    @staticmethod
    def _root_size(bit_lengths: Dict[int, int]) -> int:
        """The root size giving the shortest total code for values whose
        bit lengths have this histogram. A value of ``n`` bits costs
        ``r + 1`` bits if ``n <= r`` and ``2n - r`` otherwise."""
        def cost(r: int) -> int:
            return sum(c * (r + 1 if n <= r else 2 * n - r) for n, c in bit_lengths.items())
        return min(range(16), key=cost) if bit_lengths else 1

    def build(self) -> bytes:
        if not self.hits:
            return b""
        keys = sorted(self.hits)  # by word bytes, body (0) before title (1)

        # Pick the root sizes that minimise the WLC size; hhc.exe's choices
        # for the Excel 2013 docs (1, 1, 5) are exactly these optimums.
        doc_h: Dict[int, int] = {}
        cnt_h: Dict[int, int] = {}
        loc_h: Dict[int, int] = {}
        for k in keys:
            last = 0
            for t in sorted(self.hits[k]):
                n = (t - last).bit_length()
                doc_h[n] = doc_h.get(n, 0) + 1
                last = t
                p = self.hits[k][t]
                n = len(p).bit_length()
                cnt_h[n] = cnt_h.get(n, 0) + 1
                prev = 0
                for x in p:
                    n = (x - prev).bit_length()
                    loc_h[n] = loc_h.get(n, 0) + 1
                    prev = x
        dr, cr, lr = self._root_size(doc_h), self._root_size(cnt_h), self._root_size(loc_h)

        out = bytearray(HEADER_SIZE)
        leaves: List[Tuple[bytes, int]] = []  # (last word, offset)
        free_last = 0

        node = bytearray(8)
        node_words: List[bytes] = []
        wlc = bytearray()
        prev_word = b""

        def flush_leaf() -> None:
            nonlocal node, wlc, prev_word, free_last
            out.extend(wlc)
            leaf_off = len(out)
            if leaves:  # link the previous leaf to this one
                struct.pack_into("<I", out, leaves[-1][1], leaf_off)
            free = NODE_SIZE - len(node)
            struct.pack_into("<IHH", node, 0, 0, 0, free)
            node.extend(bytes(free))
            out.extend(node)
            leaves.append((node_words[-1], leaf_off))
            free_last = free
            node = bytearray(8)
            node_words.clear()
            wlc = bytearray()
            prev_word = b""

        def wlc_block(per_topic: Dict[int, List[int]]) -> bytes:
            bw = BitWriter()
            last_t = 0
            for t in sorted(per_topic):
                bw.write(*sr_bits(t - last_t, dr))
                last_t = t
                pos = per_topic[t]
                bw.write(*sr_bits(len(pos), cr))
                prev = 0
                for x in pos:
                    bw.write(*sr_bits(x - prev, lr))
                    prev = x
                bw.align()
            return bytes(bw.out)

        def entries_for(word: bytes, items, prev: bytes) -> List[bytes]:
            """Leaf entries for one word, minus the trailing WLC offset/size."""
            share = 0
            while share < min(len(word), len(prev)) and word[share] == prev[share]:
                share += 1
            out_e = []
            for ctx, per_topic, _block in items:
                suffix = word[share:]
                out_e.append(bytes([len(suffix) + 1, share]) + suffix + bytes([ctx]) +
                             encint_le(len(per_topic)))
                share = len(word)  # a second entry repeats the whole word
            return out_e

        # A word's body and title entries always share a leaf: simple readers
        # (chmlib's search among them) don't look for the second one in the
        # next leaf.
        i = 0
        while i < len(keys):
            word = keys[i][0]
            j = i
            while j < len(keys) and keys[j][0] == word:
                j += 1
            items = []
            for _w, ctx in keys[i:j]:
                per_topic = self.hits[(word, ctx)]
                items.append((ctx, per_topic, wlc_block(per_topic)))
            heads = entries_for(word, items, prev_word)
            size = sum(len(h) + 6 + len(encint_le(len(b))) for h, (_, _, b) in zip(heads, items))
            if len(node) + size > NODE_SIZE:
                flush_leaf()
                heads = entries_for(word, items, b"")
            for head, (_ctx, _pt, block) in zip(heads, items):
                wlc_off = len(out) + len(wlc)  # the WLC data precedes its leaf
                node.extend(head + struct.pack("<IH", wlc_off, 0) + encint_le(len(block)))
                wlc.extend(block)
            node_words.append(word)
            prev_word = word
            i = j
        flush_leaf()

        # Index levels, bottom-up; each level follows its children.
        level = leaves
        depth = 1
        while len(level) > 1:
            depth += 1
            nxt: List[Tuple[bytes, int]] = []
            node = bytearray(2)
            prev = b""
            last_word = b""

            def flush_index() -> None:
                nonlocal node, prev
                free = NODE_SIZE - len(node)
                struct.pack_into("<H", node, 0, free)
                node.extend(bytes(free))
                nxt.append((last_word, len(out)))
                out.extend(node)
                node = bytearray(2)
                prev = b""

            for word, off in level:
                share = 0
                while share < min(len(word), len(prev)) and word[share] == prev[share]:
                    share += 1
                entry = bytes([len(word) - share + 1, share]) + word[share:] + \
                    struct.pack("<IH", off, 0)
                if len(node) + len(entry) > NODE_SIZE:
                    flush_index()
                    entry = bytes([len(word) + 1, 0]) + word + struct.pack("<IH", off, 0)
                node.extend(entry)
                prev = last_word = word
            flush_index()
            level = nxt
        root = level[0][1]

        distinct = {w for w, _ in keys}
        hdr = bytearray(HEADER_SIZE)
        struct.pack_into("<4sIIIIIHI", hdr, 0, b"\0\0\x28\0", self.topics, root, 0,
                         len(leaves), root, depth, 7)
        struct.pack_into("<BBBBBB", hdr, 0x1E, SCALE, dr, SCALE, cr, SCALE, lr)
        # 0x32 is 1 in every hhc.exe file seen; 0x36/0x3A are statistics on
        # duplicate words that the viewer doesn't need. hhc.exe splits the
        # total length over 0x4A and 0x4E in an unknown way; the sum counts.
        struct.pack_into("<IIIIIIIIIIII", hdr, 0x2E, NODE_SIZE, 1, 0, 0,
                         max(len(w) for w in distinct), self.total_words, len(distinct),
                         self.total_length, 0, sum(len(w) for w, _ in keys), free_last, 0)
        struct.pack_into("<I", hdr, 0x5E, max(self.topics - 1, 0))
        struct.pack_into("<II", hdr, 0x7A, self.codepage, self.lcid)
        out[:HEADER_SIZE] = hdr
        return bytes(out)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

@dataclass
class FtsHeader:
    topics: int
    root: int
    leaves: int
    depth: int
    scales: Tuple[int, int, int]
    roots: Tuple[int, int, int]
    node_size: int
    longest: int
    total_words: int
    distinct_words: int
    codepage: int
    lcid: int


@dataclass
class FtsEntry:
    word: bytes
    context: int
    hits: Dict[int, List[int]] = field(default_factory=dict)


class FtsReader:
    def __init__(self, data: bytes) -> None:
        if len(data) < HEADER_SIZE or data[:4] != b"\0\0\x28\0":
            raise ValueError("not a $FIftiMain file")
        self.data = data
        (_sig, topics, root, _u1, leaves, _root2, depth, _u2) = struct.unpack_from(
            "<4sIIIIIHI", data, 0)
        s = struct.unpack_from("<BBBBBB", data, 0x1E)
        node_size, = struct.unpack_from("<I", data, 0x2E)
        longest, total, distinct = struct.unpack_from("<III", data, 0x3E)
        codepage, lcid = struct.unpack_from("<II", data, 0x7A)
        self.header = FtsHeader(topics, root, leaves, depth, (s[0], s[2], s[4]),
                                (s[1], s[3], s[5]), node_size, longest, total, distinct,
                                codepage, lcid)

    def _word(self, pos: int, prev: bytes) -> Tuple[bytes, int]:
        ln, share = self.data[pos], self.data[pos + 1]
        word = prev[:share] + self.data[pos + 2:pos + 1 + ln]
        return word, pos + 1 + ln

    def index_node(self, off: int) -> List[Tuple[bytes, int]]:
        d, n = self.data, self.header.node_size
        free, = struct.unpack_from("<H", d, off)
        pos, end, prev, out = off + 2, off + n - free, b"", []
        while pos < end:
            word, pos = self._word(pos, prev)
            child, = struct.unpack_from("<I", d, pos)
            pos += 6
            out.append((word, child))
            prev = word
        return out

    def leaf_node(self, off: int) -> Tuple[int, List[Tuple[bytes, int, int, int, int]]]:
        """Returns (next leaf, [(word, context, topic count, wlc offset, wlc size)])."""
        d, n = self.data, self.header.node_size
        nxt, _unk, free = struct.unpack_from("<IHH", d, off)
        pos, end, prev, out = off + 8, off + n - free, b"", []
        while pos < end:
            word, pos = self._word(pos, prev)
            ctx = d[pos]
            count, pos = decint_le(d, pos + 1)
            wlc_off, = struct.unpack_from("<I", d, pos)
            size, pos = decint_le(d, pos + 6)
            out.append((word, ctx, count, wlc_off, size))
            prev = word
        return nxt, out

    def first_leaf(self) -> int:
        off, depth = self.header.root, self.header.depth
        while depth > 1:
            off = self.index_node(off)[0][1]
            depth -= 1
        return off

    def wlc(self, count: int, off: int, size: int) -> Dict[int, List[int]]:
        dr, cr, lr = self.header.roots
        br = BitReader(self.data[off:off + size])
        hits: Dict[int, List[int]] = {}
        topic = 0
        for _ in range(count):
            topic += br.sr(dr)
            n = br.sr(cr)
            pos, loc = [], 0
            for _ in range(n):
                loc += br.sr(lr)
                pos.append(loc)
            hits[topic] = pos
            br.align()
        return hits

    def entries(self, with_hits: bool = True):
        off = self.first_leaf()
        while off:
            off, items = self.leaf_node(off)
            for word, ctx, count, wlc_off, size in items:
                e = FtsEntry(word, ctx)
                if with_hits:
                    e.hits = self.wlc(count, wlc_off, size)
                yield e

    def lookup(self, word: str) -> Dict[int, List[int]]:
        """Topics containing ``word`` (title or body) -> positions, via the tree."""
        key = fold_word(word).encode("cp%d" % self.header.codepage, errors="ignore")
        off, depth = self.header.root, self.header.depth
        while depth > 1:
            for w, child in self.index_node(off):
                if w >= key:
                    off = child
                    break
            else:
                return {}
            depth -= 1
        hits: Dict[int, List[int]] = {}
        while off:
            nxt, items = self.leaf_node(off)
            for w, _ctx, count, wlc_off, size in items:
                if w == key:
                    for t, p in self.wlc(count, wlc_off, size).items():
                        hits.setdefault(t, []).extend(p)
                elif w > key:
                    return hits
            off = nxt
        return hits

# ---------------------------------------------------------------------------
# $OBJINST
# ---------------------------------------------------------------------------

def _guid(s: str) -> bytes:
    import uuid
    return uuid.UUID(s).bytes_le


def build_objinst(codepage: int = 1252, lcid: int = 0x0409) -> bytes:
    """$OBJINST: the settings of the search engine's character table and
    word breaker. The HTML Help viewer's Search tab finds nothing without
    it, even with a valid $FIftiMain (checked with Microsoft's
    hhctrl.ocx). hhc.exe writes the same bytes for every Western project,
    from 1999 to the 2013 Office docs; this reproduces them. The character
    table is the code page 1252 one, so for other code pages only ASCII
    words are matched reliably.
    """
    table = (_CP1252_TABLE +
             bytes.fromhex("c66165e66165df73738c6f659c6f65"))  # ligatures: ae ae ss oe oe
    breaker = (_guid("8FA0D5A8-DEDF-11D0-9A61-00C04FB68BF7") +
               struct.pack("<IIIII", 0x04000000, 1, codepage, lcid, 0))
    chars = (_guid("4662DAAF-D393-11D0-9A56-00C04FB68BF7") +
             struct.pack("<IIIIIIIIHI", 0x04000000, 11, codepage, lcid, 0, 0, 0x00145555,
                         0x00000A0F, 0x0100, 0x00030005) + bytes(26) + table + breaker)
    engine = (_guid("4662DAB0-D393-11D0-9A56-00C04FB68B66") +
              struct.pack("<IIIII", 666, codepage, lcid, 10031, 0))
    head = struct.pack("<IIIIII", 0x04000000, 2, 24, len(chars), 24 + len(chars), len(engine))
    return head + chars + engine


# Character table (256 records of 10 bytes) for code page 1252, as hhc.exe
# writes it; see build_objinst.
_CP1252_TABLE = bytes.fromhex(
    "00000000000000000000070001000101010100000000020002020202000000000300030303030000"
    "00000400040404040000000005000505050500000000060006060606000000000700070707070000"
    "000008000808080800000000090009090909000000000a000a0a0a0a000000000b000b0b0b0b0000"
    "00000c000c0c0c0c000000000d000d0d0d0d000000000e000e0e1414000000000f000f0f0f0f0000"
    "00001000101010100000000011001111111100000000120012121212000000001300131313130000"
    "00002000141414140000000015001515151500000000160016161616000000001700171717170000"
    "000018001818181800000000190019191919000000001a001a1a1a1a000000001b001b1b1b1b0000"
    "00001c001c1c1c1c000000001d001d1d1d1d000000001e001e1e1e1e000000001f001f1f1f1f0000"
    "00002000202020200000000023002121212100000000280022222222000000002d00232323230000"
    "000032002424242400000000370025252525000000003c0026262626000006004100272727270000"
    "0000460028282828000000004b00292929290000090050002a2a2a2a0000000055002b2b2b2b0000"
    "04005a002c2c2c2c000000005f002d2d2d2d0000050064002e2e2e2e0000000069002f2f2f2f0000"
    "0300600430303030000003006a043131313100000300740432323232000003007e04333333330000"
    "030088043434343400000300920435353535000003009c043636363600000300a604373737370000"
    "0300b0043838383800000300ba0439393939000000006e003a3a3a3a0000000073003b3b3b3b0000"
    "000078003c3c3c3c000000007d003d3d3d3d0000000082003e3e3e3e0000090087003f3f3f3f0000"
    "00008c004040404000000200ce046141414100000200e2046242424200000200f604634343430000"
    "02000a0564444444000002001e056545454500000200320566464646000002004605674747470000"
    "02005a0568484848000002006e05694949490000020082056a4a4a4a0000020096056b4b4b4b0000"
    "0200aa056c4c4c4c00000200be056d4d4d4d00000200d2056e4e4e4e00000200e6056f4f4f4f0000"
    "0200fa0570505050000002000e067151515100000200220672525252000002003606735353530000"
    "02004a0674545454000002005e067555555500000200720676565656000002008606775757570000"
    "02009a067858585800000200ae067959595900000200c2067a5a5a5a0000000091005b5b5b5b0000"
    "000096005c5c5c5c000000009b005d5d5d5d00000000a0005e5e5e5e00000100a5005f5f5f5f0000"
    "0000aa006060606000000100ce046161616100000100e2046262626200000100f604636363630000"
    "01000a0564646464000001001e056565656500000100320566666666000001004605676767670000"
    "01005a0568686868000001006e05696969690000010082056a6a6a6a0000010096056b6b6b6b0000"
    "0100aa056c6c6c6c00000100be056d6d6d6d00000100d2056e6e6e6e00000100e6056f6f6f6f0000"
    "0100fa0570707070000001000e067171717100000100220672727272000001003606737373730000"
    "01004a0674747474000001005e067575757500000100720676767676000001008606777777770000"
    "01009a067878787800000100ae067979797900000100c2067a7a7a7a00000000af007b7b7b7b0000"
    "0000b4007c7c7c7c00000000b9007d7d7d7d00000000be007e7e7e7e00000000bf007f7f7f7f0000"
    "0000c0008080202000000000c1008181202000000000c3008282e2e200000000c8008383c4c40000"
    "0000cd008484e3e300000000d2008585c9c900000000d7008686a0a000000000dc008787e0e00000"
    "0000e10088885e5e00000000e6008989e4e4000002003606738a202000000000f0008b8bdcdc0000"
    "0c00e6056f8ccece00000000f6008d8d202000000000f7008e8e202000000000f8008f8f20200000"
    "0000f9009090202000000004fa009191d4d400000005ff009292d5d50000000604019393d2d20000"
    "000709019494d3d3000000010e019595a5a50000000213019696d0d00000000318019797d1d10000"
    "00001d0198987e7e0000000022019999aaaa000002003606739a20200000000031019b9bdddd0000"
    "0c00e6056f9ccfcf0000000037019d9d20200000000038019e9e202000000200ae06799fd9d90000"
    "00003c01a0a0a0a0000000004001a1a1c1c1000000004501a2a2a2a2000000004a01a3a3a3a30000"
    "00004f01a4a4dbdb000000005401a5a5b4b4000000005901a6a62020000000005e01a7a7a4a40000"
    "00006301a8a8acac000000006801a9a9a9a9000000006d01aaaabbbb000000007201ababc7c70000"
    "00007701acacc2c2000000007c01adad2d2d000000008101aeaea8a8000000008601afaff8f80000"
    "00008b01b0b0a1a1000000009001b1b1b1b1000000009501b2b22020000000009a01b3b320200000"
    "00009f01b4b4abab00000000a401b5b5b5b500000000a901b6b6a6a600000000ae01b7b7e1e10000"
    "0000b301b8b8fcfc00000000b801b9b9202000000000bd01bababcbc00000000c201bbbbc8c80000"
    "0000c701bcbc202000000000cc01bdbd202000000000d101bebe202000000000d601bfbfc0c00000"
    "0200ce0461c0cbcb00000200ce0461c1e7e700000200ce0461c2e5e500000200ce0461c3cccc0000"
    "0200ce0461c4808000000200ce0461c5818100000c00ce0461c6aeae00000200f60463c782820000"
    "02001e0565c8e9e9000002001e0565c98383000002001e0565cae6e6000002001e0565cbe8e80000"
    "02006e0569cceded000002006e0569cdeaea000002006e0569ceebeb000002006e0569cfecec0000"
    "02000a0564d0202000000200d2056ed1848400000200e6056fd2f1f100000200e6056fd3eeee0000"
    "0200e6056fd4efef00000200e6056fd5cdcd00000200e6056fd6858500000000db01d7d720200000"
    "0200e6056fd8afaf000002005e0675d9f4f4000002005e0675daf2f2000002005e0675dbf3f30000"
    "02005e0675dc868600000200ae0679dd2020000002004204dede202000000c00360673dfa7a70000"
    "0200ce0461e0888800000200ce0461e1878700000200ce0461e2898900000200ce0461e38b8b0000"
    "0200ce0461e48a8a00000200ce0461e58c8c00000c00ce0461e6bebe00000200f60463e78d8d0000"
    "02001e0565e88f8f000002001e0565e98e8e000002001e0565ea9090000002001e0565eb91910000"
    "02006e0569ec9393000002006e0569ed9292000002006e0569ee9494000002006e0569ef95950000"
    "02000a056ff0202000000200d2056ef1969600000200e6056ff2989800000200e6056ff397970000"
    "0200e6056ff4999900000200e6056ff59b9b00000200e6056ff69a9a000000006600f7f7d6d60000"
    "0200e6056ff8bfbf000002005e0675f99d9d000002005e0675fa9c9c000002005e0675fb9e9e0000"
    "02005e0675fc9f9f00000200ae0679fd2020000002004c04fefe202000000200ae0679ffd8d80000"
)
