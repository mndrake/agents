"""CHM reader: headers, directory, section 0 and LZX-compressed section 1.

Used by ``chmc list``, ``chmc extract`` and ``chmc verify``.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import lzxd

MSC = "::DataSpace/Storage/MSCompressed/"
RESET_TABLE = MSC + "Transform/{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}/InstanceData/ResetTable"


class CHMFormatError(ValueError):
    pass


@dataclass
class Entry:
    name: str
    section: int
    offset: int
    length: int


@dataclass
class Chunk:
    number: int
    kind: str                  # "PMGL" / "PMGI" / other signature
    free_space: int
    entry_count: int
    quickref_count: Optional[int]
    prev: Optional[int] = None
    next: Optional[int] = None
    names: List[str] = field(default_factory=list)
    children: List[int] = field(default_factory=list)  # PMGI only


def _encint(buf: bytes, pos: int):
    value = 0
    while True:
        if pos >= len(buf):
            raise CHMFormatError("ENCINT runs past end of chunk")
        b = buf[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos


class CHMFile:
    def __init__(self, path: str) -> None:
        with open(path, "rb") as fh:
            self.data = data = fh.read()
        self.path = path
        if data[:4] != b"ITSF":
            raise CHMFormatError("not a CHM/ITSF file (missing 'ITSF' signature)")
        (self.version, self.header_len, self.unknown1) = struct.unpack_from("<III", data, 4)
        self.timestamp, = struct.unpack_from(">I", data, 0x10)
        self.lcid, = struct.unpack_from("<I", data, 0x14)
        self.guid1 = data[0x18:0x28]
        self.guid2 = data[0x28:0x38]
        (self.hs0_off, self.hs0_len, self.dir_off, self.dir_len) = struct.unpack_from("<QQQQ", data, 0x38)
        if self.version >= 3:
            self.content_off, = struct.unpack_from("<Q", data, 0x58)
        else:
            self.content_off = self.dir_off + self.dir_len
        hs0 = data[self.hs0_off:self.hs0_off + self.hs0_len]
        self.hs0_magic, = struct.unpack_from("<I", hs0, 0)
        self.hs0_filesize, = struct.unpack_from("<Q", hs0, 8)

        d = data[self.dir_off:self.dir_off + self.dir_len]
        if d[:4] != b"ITSP":
            raise CHMFormatError("missing 'ITSP' directory header")
        self.itsp = dict(zip(
            ("version", "header_len", "unknown_0a", "chunk_size", "density", "depth",
             "root_index", "first_pmgl", "last_pmgl", "unknown_m1", "num_chunks", "lcid"),
            struct.unpack_from("<IIIIIIiiiiII", d, 4)))
        self.itsp_guid = d[0x34:0x44]
        hl, cs = self.itsp["header_len"], self.itsp["chunk_size"]
        self.chunks: List[Chunk] = []
        self.entries: List[Entry] = []
        for n in range(self.itsp["num_chunks"]):
            self.chunks.append(self._parse_chunk(n, d[hl + n * cs:hl + (n + 1) * cs]))
        self.by_name: Dict[str, Entry] = {e.name: e for e in self.entries}
        self._sec1: Optional[bytes] = None
        self.lzx_stats: Counter = Counter()

    # -- directory -------------------------------------------------------
    def _parse_chunk(self, n: int, c: bytes) -> Chunk:
        sig = c[:4].decode("latin-1")
        if sig not in ("PMGL", "PMGI"):
            return Chunk(n, sig, 0, 0, None)
        free, = struct.unpack_from("<I", c, 4)
        qr_count = struct.unpack_from("<H", c, len(c) - 2)[0] if len(c) >= 2 else None
        end = len(c) - free
        if sig == "PMGL":
            _, prev, nxt = struct.unpack_from("<Iii", c, 8)
            pos = 0x14
            ch = Chunk(n, sig, free, 0, qr_count, prev, nxt)
            while pos < end:
                ln, pos = _encint(c, pos)
                name = c[pos:pos + ln].decode("utf-8", "replace")
                pos += ln
                sec, pos = _encint(c, pos)
                off, pos = _encint(c, pos)
                size, pos = _encint(c, pos)
                self.entries.append(Entry(name, sec, off, size))
                ch.names.append(name)
        else:
            pos = 8
            ch = Chunk(n, sig, free, 0, qr_count)
            while pos < end:
                ln, pos = _encint(c, pos)
                ch.names.append(c[pos:pos + ln].decode("utf-8", "replace"))
                pos += ln
                child, pos = _encint(c, pos)
                ch.children.append(child)
        ch.entry_count = len(ch.names)
        return ch

    # -- content ---------------------------------------------------------
    def read_section0(self, name: str) -> bytes:
        e = self.by_name[name]
        start = self.content_off + e.offset
        return self.data[start:start + e.length]

    @property
    def control_data(self) -> dict:
        raw = self.read_section0(MSC + "ControlData")
        count, sig, version, reset, window, per_reset = struct.unpack_from("<I4sIIII", raw)
        mult = 0x8000 if version == 2 else 1
        return {"dwords": count, "signature": sig, "version": version,
                "reset_interval": reset * mult, "window_size": window * mult,
                "windows_per_reset": per_reset, "raw": raw}

    @property
    def reset_table(self) -> dict:
        raw = self.read_section0(RESET_TABLE)
        version, count, esize, hlen = struct.unpack_from("<IIII", raw)
        ulen, clen, block = struct.unpack_from("<QQQ", raw, 16)
        offsets = [struct.unpack_from("<Q", raw, hlen + i * esize)[0] for i in range(count)]
        return {"version": version, "count": count, "entry_size": esize, "header_len": hlen,
                "uncompressed_len": ulen, "compressed_len": clen, "block_size": block,
                "offsets": offsets}

    def _lzx_params(self):
        cd, rt = self.control_data, self.reset_table
        # Same interpretation as libmspack: the decoder resets every
        # reset_interval bytes of output, i.e. every reset_interval / 32K frames.
        reset_frames = max(1, cd["reset_interval"] // lzxd.FRAME_SIZE)
        return cd, rt, reset_frames

    def read(self, name: str) -> bytes:
        e = self.by_name[name]
        if e.section == 0:
            return self.read_section0(name)
        if e.length == 0:
            return b""
        if self._sec1 is not None:
            return self._sec1[e.offset:e.offset + e.length]
        cd, rt, reset_frames = self._lzx_params()
        content = self.read_section0(MSC + "Content")
        first = e.offset // lzxd.FRAME_SIZE
        last = (e.offset + e.length - 1) // lzxd.FRAME_SIZE
        data = lzxd.decompress_frames(content, rt["offsets"], rt["uncompressed_len"],
                                      cd["window_size"], reset_frames, first, last,
                                      self.lzx_stats)
        start = e.offset - first * lzxd.FRAME_SIZE
        return data[start:start + e.length]

    def decompress_all(self) -> bytes:
        if self._sec1 is None:
            cd, rt, reset_frames = self._lzx_params()
            content = self.read_section0(MSC + "Content")
            self._sec1 = lzxd.decompress_frames(content, rt["offsets"], rt["uncompressed_len"],
                                                cd["window_size"], reset_frames,
                                                stats=self.lzx_stats)
        return self._sec1


def read_directory(path: str) -> List[Entry]:
    return CHMFile(path).entries
