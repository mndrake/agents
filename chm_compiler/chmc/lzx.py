"""LZX compressor producing the frame layout used by CHM files.

CHM files store their content in an "MSCompressed" section: an LZX stream cut
into 32 KiB frames.  Every frame starts on a 16-bit boundary so a reader can
jump straight to it using the ResetTable, and the decoder state is fully reset
every ``reset_interval`` bytes.

This module implements a straightforward encoder:

* greedy LZ77 match finding with hash chains (matches never cross a frame
  boundary and never reach back past the last reset point),
* one *verbatim* block per frame with length-limited canonical Huffman trees,
* optional *uncompressed* blocks (``level=0``) for fastest builds.

The output is readable by Microsoft's HTML Help viewer, chmlib, libmspack and
7-Zip.
"""

from __future__ import annotations

import heapq
import struct
from dataclasses import dataclass, field
from typing import List, Sequence

FRAME_SIZE = 0x8000
NUM_CHARS = 256
MIN_MATCH = 3          # LZX allows 2, but 3 is what's worth encoding
MAX_MATCH = 257
NUM_SECONDARY_LENGTHS = 249
PRETREE_NUM_ELEMENTS = 20

BLOCKTYPE_VERBATIM = 1
BLOCKTYPE_UNCOMPRESSED = 3


def _position_tables(num_slots: int = 51):
    extra_bits = []
    for i in range(num_slots):
        extra_bits.append(0 if i < 4 else min((i - 2) // 2, 17))
    base = [0]
    for i in range(1, num_slots):
        base.append(base[-1] + (1 << extra_bits[i - 1]))
    return extra_bits, base


EXTRA_BITS, POSITION_BASE = _position_tables()


def num_position_slots(window_bits: int) -> int:
    if window_bits == 20:
        return 42
    if window_bits == 21:
        return 50
    return window_bits * 2


def _slot_for(formatted_offset: int) -> int:
    # Binary search over POSITION_BASE.
    lo, hi = 0, len(POSITION_BASE) - 1
    while lo < hi:
        mid = (lo + hi + 1) >> 1
        if POSITION_BASE[mid] <= formatted_offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


# Pre-computed slot lookup for all offsets a 64 KiB window can produce.
_SLOT_CACHE: List[int] = []


def _slot_lookup(formatted_offset: int) -> int:
    if formatted_offset < len(_SLOT_CACHE):
        return _SLOT_CACHE[formatted_offset]
    return _slot_for(formatted_offset)


def _fill_slot_cache(limit: int) -> None:
    if len(_SLOT_CACHE) >= limit:
        return
    _SLOT_CACHE.clear()
    slot = 0
    for off in range(limit):
        while slot + 1 < len(POSITION_BASE) and POSITION_BASE[slot + 1] <= off:
            slot += 1
        _SLOT_CACHE.append(slot)


# ---------------------------------------------------------------------------
# Bit output
# ---------------------------------------------------------------------------

class BitWriter:
    """Writes bits MSB-first into little-endian 16-bit words (LZX order)."""

    __slots__ = ("out", "acc", "nbits")

    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, value: int, nbits: int) -> None:
        if nbits == 0:
            return
        self.acc = (self.acc << nbits) | (value & ((1 << nbits) - 1))
        self.nbits += nbits
        while self.nbits >= 16:
            self.nbits -= 16
            word = (self.acc >> self.nbits) & 0xFFFF
            self.out.append(word & 0xFF)
            self.out.append(word >> 8)
        self.acc &= (1 << self.nbits) - 1

    def align16(self) -> None:
        if self.nbits:
            self.write(0, 16 - self.nbits)

    def write_bytes(self, data: bytes) -> None:
        assert self.nbits == 0
        self.out += data


# ---------------------------------------------------------------------------
# Huffman helpers
# ---------------------------------------------------------------------------

def huffman_lengths(freqs: Sequence[int], max_len: int, min_symbols: int = 2) -> List[int]:
    """Return code lengths for ``freqs`` limited to ``max_len`` bits.

    Guarantees at least ``min_symbols`` symbols get a code (LZX decoders reject
    trees with a single code), unless all frequencies are zero and
    ``min_symbols`` is 0.
    """
    freqs = list(freqs)
    n = len(freqs)
    used = [i for i, f in enumerate(freqs) if f > 0]
    if len(used) < min_symbols:
        for i in range(n):
            if len(used) >= min_symbols:
                break
            if freqs[i] == 0:
                freqs[i] = 1
                used.append(i)
    if not used:
        return [0] * n

    while True:
        heap = [(f, i, (i,)) for i, f in enumerate(freqs) if f > 0]
        heapq.heapify(heap)
        lengths = [0] * n
        if len(heap) == 1:
            lengths[heap[0][1]] = 1
            return lengths
        tiebreak = n
        while len(heap) > 1:
            f1, _, s1 = heapq.heappop(heap)
            f2, _, s2 = heapq.heappop(heap)
            for s in s1:
                lengths[s] += 1
            for s in s2:
                lengths[s] += 1
            heapq.heappush(heap, (f1 + f2, tiebreak, s1 + s2))
            tiebreak += 1
        if max(lengths) <= max_len:
            return lengths
        # Flatten the distribution and try again.
        freqs = [(f >> 1) | 1 if f > 0 else 0 for f in freqs]


def canonical_codes(lengths: Sequence[int]) -> List[int]:
    codes = [0] * len(lengths)
    code = 0
    for bits in range(1, 17):
        for sym, ln in enumerate(lengths):
            if ln == bits:
                codes[sym] = code
                code += 1
        code <<= 1
    return codes


# ---------------------------------------------------------------------------
# Match finding
# ---------------------------------------------------------------------------

def _find_tokens(data: bytes, start: int, end: int, window_start: int,
                 head: dict, prev: dict, max_chain: int, nice_len: int):
    """Greedy LZ77 over ``data[start:end]``.

    ``head``/``prev`` hold hash chains of earlier positions (>= window_start).
    Yields a flat list: literal byte values (< 256) or tuples (length, offset).
    """
    tokens = []
    append = tokens.append
    i = start
    last_hashable = end - MIN_MATCH
    while i < end:
        best_len = 0
        best_off = 0
        if i <= last_hashable:
            key = data[i:i + 3]
            cand = head.get(key)
            if cand is not None:
                max_len = end - i
                if max_len > MAX_MATCH:
                    max_len = MAX_MATCH
                chain = max_chain
                while cand is not None and cand >= window_start and chain:
                    chain -= 1
                    # quick reject on the byte that would extend best match
                    if best_len == 0 or (best_len < max_len and
                                         data[cand + best_len] == data[i + best_len]):
                        ln = 3
                        while ln + 16 <= max_len and data[cand + ln:cand + ln + 16] == data[i + ln:i + ln + 16]:
                            ln += 16
                        while ln < max_len and data[cand + ln] == data[i + ln]:
                            ln += 1
                        if ln > best_len:
                            best_len = ln
                            best_off = i - cand
                            if ln >= nice_len or ln == max_len:
                                break
                    cand = prev.get(cand)
            # insert current position
            old = head.get(key)
            if old is not None:
                prev[i] = old
            head[key] = i
        if best_len >= MIN_MATCH:
            append((best_len, best_off))
            # insert the positions covered by the match
            stop = i + best_len
            j = i + 1
            lim = min(stop, last_hashable + 1)
            while j < lim:
                key = data[j:j + 3]
                old = head.get(key)
                if old is not None:
                    prev[j] = old
                head[key] = j
                j += 1
            i = stop
        else:
            append(data[i])
            i += 1
    return tokens


# ---------------------------------------------------------------------------
# Tree encoding
# ---------------------------------------------------------------------------

def _write_tree(bw: BitWriter, new_lens: Sequence[int], prev_lens: List[int],
                first: int, last: int) -> None:
    """Encode lengths[first:last] with a pretree, delta against prev_lens."""
    codes = []  # (pretree symbol, extra value, extra bits)
    i = first
    while i < last:
        if new_lens[i] == 0:
            run = 1
            while i + run < last and new_lens[i + run] == 0:
                run += 1
            if run >= 4:
                while run >= 20:
                    n = min(run, 51)
                    codes.append((18, n - 20, 5))
                    run -= n
                    i += n
                if run >= 4:
                    codes.append((17, run - 4, 4))
                    i += run
                    run = 0
                for _ in range(run):
                    codes.append(((prev_lens[i] - 0) % 17, 0, 0))
                    i += 1
                continue
        codes.append(((prev_lens[i] - new_lens[i]) % 17, 0, 0))
        i += 1

    freqs = [0] * PRETREE_NUM_ELEMENTS
    for sym, _, _ in codes:
        freqs[sym] += 1
    pre_lens = huffman_lengths(freqs, 15)
    pre_codes = canonical_codes(pre_lens)
    for ln in pre_lens:
        bw.write(ln, 4)
    for sym, extra, nbits in codes:
        bw.write(pre_codes[sym], pre_lens[sym])
        if nbits:
            bw.write(extra, nbits)
    for k in range(first, last):
        prev_lens[k] = new_lens[k]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class LZXResult:
    data: bytes
    frame_offsets: List[int] = field(default_factory=list)  # compressed offset of each frame
    uncompressed_length: int = 0


def compress(data: bytes, level: int = 6, window_bits: int = 16,
             reset_interval: int = 0x10000) -> LZXResult:
    """Compress ``data`` into a CHM-style LZX stream.

    ``level`` 0 stores uncompressed blocks; higher levels search longer hash
    chains.  ``reset_interval`` must be a multiple of ``FRAME_SIZE``.
    """
    assert reset_interval % FRAME_SIZE == 0
    window_size = 1 << window_bits
    assert reset_interval <= window_size
    main_elements = NUM_CHARS + num_position_slots(window_bits) * 8
    frames_per_reset = reset_interval // FRAME_SIZE
    _fill_slot_cache(window_size + 3)

    max_chain = {0: 0, 1: 4, 2: 8, 3: 12, 4: 16, 5: 24, 6: 32, 7: 64, 8: 128, 9: 256}.get(level, 32)
    nice_len = {1: 16, 2: 24, 3: 32, 4: 48, 5: 64, 6: 128}.get(level, MAX_MATCH)

    real_len = len(data)
    nframes = (real_len + FRAME_SIZE - 1) // FRAME_SIZE
    # The final frame is padded with zeros: some readers (chmlib) always
    # decode a full 32 KiB frame.
    padded = bytes(data) + bytes(nframes * FRAME_SIZE - real_len)

    bw = BitWriter()
    offsets: List[int] = []
    main_prev = [0] * main_elements
    len_prev = [0] * NUM_SECONDARY_LENGTHS
    head: dict = {}
    prev: dict = {}
    group_start = 0

    for f in range(nframes):
        fstart = f * FRAME_SIZE
        fend = fstart + FRAME_SIZE
        offsets.append(len(bw.out))
        if f % frames_per_reset == 0:
            # LZX reset: fresh state, header bit (no E8 translation)
            main_prev = [0] * main_elements
            len_prev = [0] * NUM_SECONDARY_LENGTHS
            head = {}
            prev = {}
            group_start = fstart
            bw.write(0, 1)

        if level <= 0:
            bw.write(BLOCKTYPE_UNCOMPRESSED, 3)
            bw.write(FRAME_SIZE >> 8, 16)
            bw.write(FRAME_SIZE & 0xFF, 8)
            # 1..16 bits of padding to reach a 16-bit boundary
            bw.write(0, 16 - bw.nbits if bw.nbits else 16)
            bw.write_bytes(struct.pack("<III", 1, 1, 1))
            bw.write_bytes(padded[fstart:fend])
            continue

        # Matches may not run across the end of the real data (some decoders
        # truncate the final frame to the true length).
        if fstart < real_len < fend:
            tokens = _find_tokens(padded, fstart, real_len, group_start, head, prev,
                                  max_chain, nice_len)
            tokens += _find_tokens(padded, real_len, fend, real_len, {}, {},
                                   max_chain, nice_len)
        else:
            tokens = _find_tokens(padded, fstart, fend, group_start, head, prev,
                                  max_chain, nice_len)

        # Translate tokens into symbols & gather statistics.
        main_freq = [0] * main_elements
        len_freq = [0] * NUM_SECONDARY_LENGTHS
        syms = []
        for t in tokens:
            if t.__class__ is int:
                main_freq[t] += 1
                syms.append(t)
            else:
                length, offset = t
                formatted = offset + 2
                slot = _slot_lookup(formatted)
                lh = length - 2
                if lh > 7:
                    lh = 7
                main_sym = NUM_CHARS + (slot << 3) + lh
                main_freq[main_sym] += 1
                footer_len = length - 9 if lh == 7 else -1
                if footer_len >= 0:
                    len_freq[footer_len] += 1
                syms.append((main_sym, footer_len, formatted - POSITION_BASE[slot],
                             EXTRA_BITS[slot]))

        main_lens = huffman_lengths(main_freq, 16)
        len_lens = huffman_lengths(len_freq, 16, min_symbols=0)
        if sum(1 for x in len_lens if x) == 1:
            len_lens = huffman_lengths(len_freq, 16, min_symbols=2)
        main_codes = canonical_codes(main_lens)
        len_codes = canonical_codes(len_lens)

        bw.write(BLOCKTYPE_VERBATIM, 3)
        bw.write(FRAME_SIZE >> 8, 16)
        bw.write(FRAME_SIZE & 0xFF, 8)
        _write_tree(bw, main_lens, main_prev, 0, NUM_CHARS)
        _write_tree(bw, main_lens, main_prev, NUM_CHARS, main_elements)
        _write_tree(bw, len_lens, len_prev, 0, NUM_SECONDARY_LENGTHS)

        write = bw.write
        for s in syms:
            if s.__class__ is int:
                write(main_codes[s], main_lens[s])
            else:
                main_sym, footer_len, verbatim, nbits = s
                write(main_codes[main_sym], main_lens[main_sym])
                if footer_len >= 0:
                    write(len_codes[footer_len], len_lens[footer_len])
                if nbits:
                    write(verbatim, nbits)
        bw.align16()

    bw.align16()
    return LZXResult(bytes(bw.out), offsets, real_len)
