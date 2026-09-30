"""Writer for the ITSF container ("InfoTech Storage Format") used by .chm files.

Layout of the file produced here::

    ITSF header (0x60)
    header section 0 (0x18)          - file size
    header section 1                 - ITSP directory header + PMGL/PMGI chunks
    content section 0                - uncompressed storage (DataSpace files,
                                       and the compressed stream itself)

Content section 1 ("MSCompressed") is an LZX stream stored as the file
``::DataSpace/Storage/MSCompressed/Content`` inside content section 0.
"""

from __future__ import annotations

import struct
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import lzx

CHUNK_SIZE = 0x1000
QUICKREF_DENSITY = 2
QUICKREF_EVERY = 1 + (1 << QUICKREF_DENSITY)

GUID_ITSF_1 = uuid.UUID("7C01FD10-7BAA-11D0-9E0C-00A0C922E6EC").bytes_le
GUID_ITSF_2 = uuid.UUID("7C01FD11-7BAA-11D0-9E0C-00A0C922E6EC").bytes_le
GUID_ITSP = uuid.UUID("5D02926A-212E-11D0-9DF9-00A0C922E6EC").bytes_le
LZX_TRANSFORM_GUID = "{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}"

MSC = "::DataSpace/Storage/MSCompressed/"
RESET_TABLE_NAME = MSC + "Transform/" + LZX_TRANSFORM_GUID + "/InstanceData/ResetTable"


def encint(value: int) -> bytes:
    """Variable-length big-endian 7-bit integer used in directory entries."""
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def sort_key(name: str) -> bytes:
    # Readers binary-search the directory with a case-insensitive compare.
    return name.encode("utf-8").lower()


@dataclass
class DirEntry:
    name: str
    section: int
    offset: int
    length: int

    def encode(self) -> bytes:
        raw = self.name.encode("utf-8")
        return (encint(len(raw)) + raw + encint(self.section) +
                encint(self.offset) + encint(self.length))


def _quickref(offsets: List[int], count: int) -> bytes:
    """Quickref area stored at the end of a PMGL/PMGI chunk."""
    refs = [offsets[i] for i in range(QUICKREF_EVERY, count, QUICKREF_EVERY)]
    return b"".join(struct.pack("<H", r) for r in reversed(refs)) + struct.pack("<H", count)


def _qr_size(count: int) -> int:
    return 2 + 2 * len(range(QUICKREF_EVERY, count, QUICKREF_EVERY))


def _pack_chunks(items: List[Tuple[str, bytes]], header_size: int) -> List[List[Tuple[str, bytes]]]:
    chunks: List[List[Tuple[str, bytes]]] = [[]]
    used = 0
    for name, enc in items:
        cur = chunks[-1]
        if cur and header_size + used + len(enc) + _qr_size(len(cur) + 1) > CHUNK_SIZE:
            chunks.append([])
            cur = chunks[-1]
            used = 0
        if header_size + len(enc) + _qr_size(1) > CHUNK_SIZE:
            raise ValueError(f"file name too long for CHM directory: {name!r}")
        cur.append((name, enc))
        used += len(enc)
    return chunks


def _build_chunk(sig: bytes, entries: List[bytes], extra_header: bytes) -> bytes:
    header_size = 8 + len(extra_header)
    offsets = []
    body = bytearray()
    for e in entries:
        offsets.append(len(body))
        body += e
    qr = _quickref(offsets, len(entries))
    free = CHUNK_SIZE - header_size - len(body)
    chunk = sig + struct.pack("<I", free) + extra_header + bytes(body)
    chunk += bytes(CHUNK_SIZE - len(chunk) - len(qr)) + qr
    assert len(chunk) == CHUNK_SIZE
    return chunk


def build_directory(entries: List[DirEntry], lcid: int) -> bytes:
    """Build the ITSP header plus PMGL listing chunks and PMGI index chunks."""
    entries = sorted(entries, key=lambda e: sort_key(e.name))
    items = [(e.name, e.encode()) for e in entries]
    pmgl_groups = _pack_chunks(items, 0x14)
    n_pmgl = len(pmgl_groups)

    chunks: List[bytes] = []
    for i, group in enumerate(pmgl_groups):
        prev_c = i - 1 if i > 0 else -1
        next_c = i + 1 if i + 1 < n_pmgl else -1
        extra = struct.pack("<Iii", 0, prev_c, next_c)
        chunks.append(_build_chunk(b"PMGL", [enc for _, enc in group], extra))

    # Index levels (PMGI) until a single chunk covers the level below.
    depth = 1
    root = -1
    level = [(group[0][0], i) for i, group in enumerate(pmgl_groups)]
    if n_pmgl > 1:
        while True:
            depth += 1
            idx_items = []
            for name, chunk_no in level:
                raw = name.encode("utf-8")
                idx_items.append((name, encint(len(raw)) + raw + encint(chunk_no)))
            groups = _pack_chunks(idx_items, 0x08)
            new_level = []
            for group in groups:
                new_level.append((group[0][0], len(chunks)))
                chunks.append(_build_chunk(b"PMGI", [enc for _, enc in group], b""))
            level = new_level
            if len(level) == 1:
                root = level[0][1]
                break

    header = b"ITSP" + struct.pack(
        "<IIIIIIiIIiII", 1, 0x54, 0x0A, CHUNK_SIZE, QUICKREF_DENSITY, depth,
        root, 0, n_pmgl - 1, -1, len(chunks), lcid)
    header += GUID_ITSP + struct.pack("<Iiii", 0x54, -1, -1, -1)
    assert len(header) == 0x54
    return header + b"".join(chunks)


def _utf16_name_list(names: List[str]) -> bytes:
    body = b""
    for n in names:
        body += struct.pack("<H", len(n)) + n.encode("utf-16-le") + b"\0\0"
    data = struct.pack("<H", len(names)) + body
    return struct.pack("<H", (len(data) + 2) // 2) + data


class ITSFWriter:
    """Collects files and writes a complete .chm/.its container."""

    def __init__(self, lcid: int = 0x0409, compression_level: int = 6,
                 timestamp: Optional[int] = None) -> None:
        self.lcid = lcid
        self.level = compression_level
        self.timestamp = int(time.time()) if timestamp is None else timestamp
        self.files: Dict[str, bytes] = {}  # name -> data
        self.order: List[str] = []         # names stored in section 1 (compressed)
        self.order0: List[str] = []        # names stored in section 0 (uncompressed)
        self._keys: set = set()

    def add(self, name: str, data: bytes, section: int = 1) -> None:
        if not name.startswith(("/", "::")):
            name = "/" + name
        key = sort_key(name)
        if key in self._keys:
            raise ValueError(f"duplicate file in CHM: {name}")
        self._keys.add(key)
        self.files[name] = bytes(data)
        (self.order0 if section == 0 else self.order).append(name)

    def build(self) -> bytes:
        # --- content section 1 (compressed) ---------------------------------
        stream = bytearray()
        entries: List[DirEntry] = []
        dirs = {"/"}
        for name in self.order:
            data = self.files[name]
            entries.append(DirEntry(name, 1, len(stream), len(data)))
            stream += data
        for name in self.order + self.order0:
            parts = name.split("/")
            for i in range(2, len(parts)):
                dirs.add("/".join(parts[:i]) + "/")
        dirs.add(RESET_TABLE_NAME.rsplit("/", 1)[0] + "/")
        for d in dirs:
            entries.append(DirEntry(d, 0, 0, 0))

        comp = lzx.compress(bytes(stream), level=self.level)

        reset_table = struct.pack("<IIII", 2, len(comp.frame_offsets), 8, 0x28)
        reset_table += struct.pack("<QQQ", len(stream), len(comp.data), lzx.FRAME_SIZE)
        reset_table += b"".join(struct.pack("<Q", o) for o in comp.frame_offsets)

        # LZXC control data: version 2, reset interval / window size in 32 KiB units.
        control = struct.pack("<I", 6) + b"LZXC" + struct.pack("<IIIII", 2, 2, 2, 1, 0)

        # --- content section 0 (uncompressed) -------------------------------
        sec0 = bytearray()

        def put0(name: str, data: bytes) -> None:
            entries.append(DirEntry(name, 0, len(sec0), len(data)))
            sec0.extend(data)

        put0("::DataSpace/NameList", _utf16_name_list(["Uncompressed", "MSCompressed"]))
        # HHC writes only the first 38 bytes of the UTF-16 GUID; mirror that.
        put0(MSC + "Transform/List", LZX_TRANSFORM_GUID.encode("utf-16-le")[:38])
        put0(MSC + "SpanInfo", struct.pack("<Q", len(stream)))
        put0(RESET_TABLE_NAME, reset_table)
        put0(MSC + "ControlData", control)
        for name in self.order0:
            put0(name, self.files[name])
        put0(MSC + "Content", comp.data)

        directory = build_directory(entries, self.lcid)

        header_len = 0x60
        hs0_off = header_len
        dir_off = hs0_off + 0x18
        content_off = dir_off + len(directory)
        total = content_off + len(sec0)

        itsf = b"ITSF" + struct.pack("<III", 3, header_len, 1)
        itsf += struct.pack(">I", self.timestamp & 0xFFFFFFFF)
        itsf += struct.pack("<I", self.lcid) + GUID_ITSF_1 + GUID_ITSF_2
        itsf += struct.pack("<QQQQQ", hs0_off, 0x18, dir_off, len(directory), content_off)
        assert len(itsf) == header_len
        hs0 = struct.pack("<IIQII", 0x01FE, 0, total, 0, 0)

        out = itsf + hs0 + directory + bytes(sec0)
        assert len(out) == total
        return out
