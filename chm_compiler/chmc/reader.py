"""Minimal CHM directory reader used by ``chmc list`` (no decompression)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List


@dataclass
class Entry:
    name: str
    section: int
    offset: int
    length: int


def _encint(buf: bytes, pos: int):
    value = 0
    while True:
        b = buf[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos


def read_directory(path: str) -> List[Entry]:
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"ITSF":
        raise ValueError("not a CHM/ITSF file")
    dir_off, dir_len = struct.unpack_from("<QQ", data, 0x48)
    itsp = data[dir_off:dir_off + dir_len]
    if itsp[:4] != b"ITSP":
        raise ValueError("missing ITSP directory header")
    hdr_len, = struct.unpack_from("<I", itsp, 8)
    chunk_size, = struct.unpack_from("<I", itsp, 0x10)
    first_pmgl, last_pmgl = struct.unpack_from("<ii", itsp, 0x20)
    entries: List[Entry] = []
    chunk_no = first_pmgl
    while chunk_no != -1:
        chunk = itsp[hdr_len + chunk_no * chunk_size:hdr_len + (chunk_no + 1) * chunk_size]
        if chunk[:4] != b"PMGL":
            raise ValueError(f"bad listing chunk {chunk_no}")
        free, = struct.unpack_from("<I", chunk, 4)
        nxt, = struct.unpack_from("<i", chunk, 0x10)
        pos, end = 0x14, chunk_size - free
        while pos < end:
            n, pos = _encint(chunk, pos)
            name = chunk[pos:pos + n].decode("utf-8", "replace")
            pos += n
            sec, pos = _encint(chunk, pos)
            off, pos = _encint(chunk, pos)
            ln, pos = _encint(chunk, pos)
            entries.append(Entry(name, sec, off, ln))
        if chunk_no == last_pmgl:
            break
        chunk_no = nxt
    return entries
