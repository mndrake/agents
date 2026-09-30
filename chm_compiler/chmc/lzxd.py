"""LZX decoder for CHM content (verbatim, aligned-offset and uncompressed blocks).

Used by ``chmc verify`` / ``chmc extract`` to read CHM files produced by any
compiler, including Microsoft's hhc.exe (which emits aligned-offset blocks and
may let blocks span frames).  Frames are decoded independently of the stream
position using the ResetTable, the same way chmlib does.
"""

from __future__ import annotations

from collections import Counter
from typing import List, Optional

from .lzx import EXTRA_BITS, NUM_CHARS, NUM_SECONDARY_LENGTHS, POSITION_BASE, num_position_slots

FRAME_SIZE = 0x8000
ALIGNED_NUM_ELEMENTS = 8


class LZXError(Exception):
    pass


class _Bits:
    __slots__ = ("data", "pos", "buf", "n")

    def __init__(self, data: bytes, pos: int) -> None:
        self.data, self.pos, self.buf, self.n = data, pos, 0, 0

    def _fill(self, need: int) -> None:
        d = self.data
        while self.n < need:
            p = self.pos
            word = (d[p] if p < len(d) else 0) | ((d[p + 1] if p + 1 < len(d) else 0) << 8)
            self.pos = p + 2
            self.buf = (self.buf << 16) | word
            self.n += 16

    def peek(self, k: int) -> int:
        if self.n < k:
            self._fill(k)
        return (self.buf >> (self.n - k)) & ((1 << k) - 1)

    def skip(self, k: int) -> None:
        self.n -= k
        self.buf &= (1 << self.n) - 1

    def read(self, k: int) -> int:
        if k == 0:
            return 0
        v = self.peek(k)
        self.skip(k)
        return v

    def align_for_raw(self) -> None:
        """Uncompressed block: discard 1-16 bits so the byte pointer is aligned."""
        # Leftover bits (< 16) all come from the last word loaded, so dropping
        # them leaves ``pos`` word-aligned; with none left, skip a whole word.
        if self.n == 0:
            self._fill(16)
        self.n = 0
        self.buf = 0


class _Tree:
    __slots__ = ("bits", "table", "lens")

    def __init__(self, lens: List[int]) -> None:
        self.lens = lens
        maxlen = max(lens) if lens else 0
        self.bits = maxlen
        if maxlen == 0:
            self.table = None
            return
        size = 1 << maxlen
        table = [-1] * size
        code = 0
        for ln in range(1, maxlen + 1):
            for sym, l in enumerate(lens):
                if l == ln:
                    shift = maxlen - ln
                    start = code << shift
                    end = (code + 1) << shift
                    if end > size:
                        raise LZXError("over-subscribed Huffman table")
                    table[start:end] = [sym] * (end - start)
                    code += 1
            code <<= 1
        self.table = table

    def decode(self, bits: _Bits) -> int:
        if self.table is None:
            raise LZXError("symbol read from an empty Huffman table")
        sym = self.table[bits.peek(self.bits)]
        if sym < 0:
            raise LZXError("invalid Huffman code")
        bits.skip(self.lens[sym])
        return sym


class LZXDecoder:
    def __init__(self, window_size: int) -> None:
        if window_size & (window_size - 1) or not (1 << 15) <= window_size <= (1 << 21):
            raise LZXError(f"unsupported LZX window size {window_size:#x}")
        self.window_size = window_size
        self.main_elements = NUM_CHARS + num_position_slots(window_size.bit_length() - 1) * 8
        self.stats: Counter = Counter()
        self.reset()

    def reset(self) -> None:
        self.window = bytearray(self.window_size)
        self.wpos = 0
        self.R = [1, 1, 1]
        self.main_lens = [0] * self.main_elements
        self.len_lens = [0] * NUM_SECONDARY_LENGTHS
        self.header_read = False
        self.intel_filesize = 0
        self.block_type = 0
        self.block_remaining = 0
        self.block_length = 0
        self.frames_read = 0
        self.main_tree = self.len_tree = self.aligned_tree = None

    def _read_lens(self, bits: _Bits, lens: List[int], first: int, last: int) -> None:
        pre = _Tree([bits.read(4) for _ in range(20)])
        x = first
        while x < last:
            z = pre.decode(bits)
            if z == 17:
                n = bits.read(4) + 4
                lens[x:x + n] = [0] * n
                x += n
            elif z == 18:
                n = bits.read(5) + 20
                lens[x:x + n] = [0] * n
                x += n
            elif z == 19:
                n = bits.read(1) + 4
                z = pre.decode(bits)
                v = (lens[x] - z) % 17
                lens[x:x + n] = [v] * n
                x += n
            else:
                lens[x] = (lens[x] - z) % 17
                x += 1
        if x > last:
            raise LZXError("tree length run overflows the table")

    def _read_block_header(self, bits: _Bits) -> None:
        if self.block_type == 3 and self.block_length & 1:
            bits.pos += 1  # odd-length uncompressed block is followed by a pad byte
        btype = bits.read(3)
        size = (bits.read(16) << 8) | bits.read(8)
        self.block_type, self.block_remaining, self.block_length = btype, size, size
        self.stats[{1: "verbatim", 2: "aligned", 3: "uncompressed"}.get(btype, f"type{btype}")] += 1
        if btype == 2:
            self.aligned_tree = _Tree([bits.read(3) for _ in range(ALIGNED_NUM_ELEMENTS)])
        if btype in (1, 2):
            self._read_lens(bits, self.main_lens, 0, NUM_CHARS)
            self._read_lens(bits, self.main_lens, NUM_CHARS, self.main_elements)
            self.main_tree = _Tree(list(self.main_lens))
            self._read_lens(bits, self.len_lens, 0, NUM_SECONDARY_LENGTHS)
            self.len_tree = _Tree(list(self.len_lens)) if any(self.len_lens) else None
        elif btype == 3:
            bits.align_for_raw()
            d, p = bits.data, bits.pos
            self.R = [int.from_bytes(d[p + 4 * i:p + 4 * i + 4], "little") for i in range(3)]
            bits.pos += 12
        else:
            raise LZXError(f"invalid block type {btype}")

    def decode_frame(self, data: bytes, pos: int, out_len: int) -> bytes:
        """Decode one frame whose compressed bits start at ``data[pos]``."""
        bits = _Bits(data, pos)
        if not self.header_read:
            if bits.read(1):
                self.intel_filesize = (bits.read(16) << 16) | bits.read(16)
                self.stats["e8-translation"] += 1
            self.header_read = True
        win, wsize = self.window, self.window_size
        frame_start = self.wpos
        todo = out_len
        while todo > 0:
            if self.block_remaining == 0:
                self._read_block_header(bits)
            run = min(self.block_remaining, todo)
            if self.block_type == 3:
                chunk = data[bits.pos:bits.pos + run]
                if len(chunk) != run:
                    raise LZXError("uncompressed block runs past end of data")
                bits.pos += run
                p = self.wpos
                first = min(run, wsize - p)
                win[p:p + first] = chunk[:first]
                win[:run - first] = chunk[first:]
                self.wpos = (p + run) % wsize
                done = run
            else:
                done = self._decode_symbols(bits, run)
            self.block_remaining -= done
            todo -= done
            if todo < 0:
                raise LZXError("match crosses a frame boundary")
        end = frame_start + out_len
        out = bytes(win[frame_start:end]) if end <= wsize else \
            bytes(win[frame_start:]) + bytes(win[:end - wsize])
        if self.intel_filesize and self.frames_read < 32768 and out_len > 10:
            out = self._undo_e8(out)
        self.frames_read += 1
        return out

    def _decode_symbols(self, bits: _Bits, run: int) -> int:
        win, wsize, R = self.window, self.window_size, self.R
        main, lent, aligned = self.main_tree, self.len_tree, self.aligned_tree
        is_aligned = self.block_type == 2
        done = 0
        wpos = self.wpos
        while done < run:
            sym = main.decode(bits)
            if sym < NUM_CHARS:
                win[wpos] = sym
                wpos += 1
                if wpos == wsize:
                    wpos = 0
                done += 1
                continue
            sym -= NUM_CHARS
            length = sym & 7
            if length == 7:
                if lent is None:
                    raise LZXError("length tree needed but empty")
                length += lent.decode(bits)
            length += 2
            slot = sym >> 3
            if slot > 2:
                extra = EXTRA_BITS[slot]
                if is_aligned and extra >= 3:
                    off = POSITION_BASE[slot] - 2 + (bits.read(extra - 3) << 3) + aligned.decode(bits)
                else:
                    off = POSITION_BASE[slot] - 2 + bits.read(extra)
                R[2], R[1], R[0] = R[1], R[0], off
            elif slot == 0:
                off = R[0]
            elif slot == 1:
                off = R[1]
                R[1], R[0] = R[0], off
            else:
                off = R[2]
                R[2], R[0] = R[0], off
            src = wpos - off
            if src < 0:
                src += wsize
            if off >= length and src + length <= wsize and wpos + length <= wsize:
                win[wpos:wpos + length] = win[src:src + length]
                wpos += length
                if wpos == wsize:
                    wpos = 0
            else:
                for _ in range(length):
                    win[wpos] = win[src]
                    wpos = (wpos + 1) % wsize
                    src = (src + 1) % wsize
            done += length
        self.wpos = wpos
        return done

    def _undo_e8(self, out: bytes) -> bytes:
        buf = bytearray(out)
        curpos = (self.frames_read * FRAME_SIZE)
        i, end = 0, len(buf) - 10
        while i < end:
            if buf[i] != 0xE8:
                i += 1
                continue
            abs_off = int.from_bytes(buf[i + 1:i + 5], "little", signed=True)
            pos = curpos + i
            if -pos <= abs_off < self.intel_filesize:
                rel = abs_off - pos if abs_off >= 0 else abs_off + self.intel_filesize
                buf[i + 1:i + 5] = (rel & 0xFFFFFFFF).to_bytes(4, "little")
            i += 5
        return bytes(buf)


def decompress_frames(content: bytes, frame_offsets: List[int], uncompressed_len: int,
                      window_size: int, reset_blocks: int, first: int = 0,
                      last: Optional[int] = None, stats: Optional[Counter] = None) -> bytes:
    """Decode frames ``first..last`` (inclusive), starting from the preceding reset point."""
    nframes = len(frame_offsets)
    if last is None:
        last = nframes - 1
    dec = LZXDecoder(window_size)
    start = first - (first % reset_blocks)
    out = bytearray()
    for f in range(start, last + 1):
        if f % reset_blocks == 0:
            dec.reset()
        want = min(FRAME_SIZE, uncompressed_len - f * FRAME_SIZE)
        piece = dec.decode_frame(content, frame_offsets[f], want)
        if f >= first:
            out += piece
    if stats is not None:
        stats.update(dec.stats)
    return bytes(out)
