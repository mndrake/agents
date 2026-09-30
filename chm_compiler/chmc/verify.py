"""``chmc verify``: check a .chm against the CHM (ITSF/LZX) specification.

The checks cover every structure chmc writes: ITSF and ITSP headers,
PMGL/PMGI directory chunks, DataSpace files, the LZX control data and reset
table, full decompression, and the #SYSTEM/#WINDOWS/#STRINGS/topic tables.
With ``--compare`` it prints the format parameters of two files side by side,
e.g. a chmc build next to a CHM produced by Microsoft's hhc.exe.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .reader import MSC, RESET_TABLE, CHMFile, CHMFormatError

GUID_ITSF_1 = uuid.UUID("7C01FD10-7BAA-11D0-9E0C-00A0C922E6EC").bytes_le
GUID_ITSF_2 = uuid.UUID("7C01FD11-7BAA-11D0-9E0C-00A0C922E6EC").bytes_le
GUID_ITSP = uuid.UUID("5D02926A-212E-11D0-9DF9-00A0C922E6EC").bytes_le
LZX_GUID = "{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}"

SYSTEM_CODES = {
    0: "Contents file", 1: "Index file", 2: "Default topic", 3: "Title", 4: "LCID/flags",
    5: "Default window", 6: "Compiled file", 7: "Binary index", 8: "Abbreviations",
    9: "Compiler version", 10: "Timestamp", 11: "Binary TOC", 12: "Info type count",
    13: "#IDXHDR copy", 14: "MSOffice extension", 15: "Info type checksum", 16: "Default font",
}

OPTIONAL_FILES = [
    ("/#SYSTEM", "project options"),
    ("/#WINDOWS", "window definitions"),
    ("/#STRINGS", "string pool"),
    ("/#TOPICS", "topic table"),
    ("/#URLTBL", "URL table"),
    ("/#URLSTR", "URL strings"),
    ("/#ITBITS", "(empty marker)"),
    ("/#IDXHDR", "index header"),
    ("/#TOCIDX", "binary table of contents"),
    ("/#IVB", "context-help IDs ([MAP])"),
    ("/#SUBSETS", "subsets"),
    ("/$FIftiMain", "full-text search index"),
    ("/$OBJINST", "ActiveX object instances"),
    ("/$WWKeywordLinks/BTree", "binary keyword index"),
    ("/$WWAssociativeLinks/BTree", "binary A-link index"),
]

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    path: str
    checks: List[Check] = field(default_factory=list)
    params: Dict[str, str] = field(default_factory=dict)

    def add(self, area: str, name: str, ok: Optional[bool], detail: str = "",
            soft: bool = False) -> None:
        status = INFO if ok is None else PASS if ok else (WARN if soft else FAIL)
        self.checks.append(Check(area, name, status, detail))

    @property
    def failures(self) -> int:
        return sum(c.status == FAIL for c in self.checks)

    @property
    def warnings(self) -> int:
        return sum(c.status == WARN for c in self.checks)


def _hexs(v: int) -> str:
    return f"0x{v:X}"


def verify(path: str, decompress: bool = True) -> Report:
    rep = Report(path)
    try:
        chm = CHMFile(path)
    except (CHMFormatError, struct.error, OSError) as exc:
        rep.add("ITSF", "file parses", False, str(exc))
        return rep
    P = rep.params

    # ---- ITSF header ----------------------------------------------------
    A = "ITSF header"
    rep.add(A, "signature 'ITSF'", True)
    rep.add(A, "version 3", chm.version == 3, f"version {chm.version}", soft=chm.version == 2)
    exp_hl = 0x60 if chm.version >= 3 else 0x58
    rep.add(A, f"header length {_hexs(exp_hl)}", chm.header_len == exp_hl, _hexs(chm.header_len))
    rep.add(A, "unknown field = 1", chm.unknown1 == 1, str(chm.unknown1), soft=True)
    rep.add(A, "ITSF GUIDs {7C01FD10/11-...}", chm.guid1 == GUID_ITSF_1 and chm.guid2 == GUID_ITSF_2)
    rep.add(A, "header section 0 follows header", chm.hs0_off == chm.header_len and chm.hs0_len == 0x18,
            f"offset {_hexs(chm.hs0_off)}, length {_hexs(chm.hs0_len)}")
    rep.add(A, "header section 0 magic 0x1FE", chm.hs0_magic == 0x1FE, _hexs(chm.hs0_magic))
    rep.add(A, "recorded file size = actual size", chm.hs0_filesize == len(chm.data),
            f"{chm.hs0_filesize:,} vs {len(chm.data):,}")
    rep.add(A, "directory follows header section 0", chm.dir_off == chm.hs0_off + chm.hs0_len)
    rep.add(A, "content section 0 follows directory", chm.content_off == chm.dir_off + chm.dir_len,
            f"{_hexs(chm.content_off)}")
    rep.add(A, "language id", None, f"{_hexs(chm.lcid)}")
    P.update({"ITSF version": str(chm.version), "ITSF header length": _hexs(chm.header_len),
              "ITSF unknown field": str(chm.unknown1),
              "ITSF GUIDs": "standard" if chm.guid1 == GUID_ITSF_1 and chm.guid2 == GUID_ITSF_2 else "non-standard",
              "Header section 0": f"{_hexs(chm.hs0_len)} bytes, magic {_hexs(chm.hs0_magic)}",
              "File layout": "standard" if
              chm.dir_off == chm.hs0_off + chm.hs0_len and chm.content_off == chm.dir_off + chm.dir_len
              else "non-standard"})

    # ---- ITSP directory -------------------------------------------------
    A = "ITSP directory"
    it = chm.itsp
    rep.add(A, "version 1", it["version"] == 1, str(it["version"]))
    rep.add(A, "header length 0x54", it["header_len"] == 0x54, _hexs(it["header_len"]))
    rep.add(A, "unknown field = 0x0A", it["unknown_0a"] == 0x0A, _hexs(it["unknown_0a"]), soft=True)
    rep.add(A, "chunk size 0x1000", it["chunk_size"] == 0x1000, _hexs(it["chunk_size"]),
            soft=True)
    rep.add(A, "quickref density 2", it["density"] == 2, str(it["density"]), soft=True)
    rep.add(A, "ITSP GUID {5D02926A-...}", chm.itsp_guid == GUID_ITSP)
    rep.add(A, "directory length = header + chunks",
            chm.dir_len == it["header_len"] + it["num_chunks"] * it["chunk_size"],
            f"{it['num_chunks']} chunks")
    pmgl = [c for c in chm.chunks if c.kind == "PMGL"]
    pmgi = [c for c in chm.chunks if c.kind == "PMGI"]
    other = [c for c in chm.chunks if c.kind not in ("PMGL", "PMGI")]
    rep.add(A, "all chunks are PMGL/PMGI", not other, ", ".join(f"#{c.number}={c.kind!r}" for c in other[:5]))
    exp_depth_ok = (it["depth"] == 1 and not pmgi and it["root_index"] == -1) or \
        (it["depth"] >= 2 and pmgi and 0 <= it["root_index"] < len(chm.chunks) and
         chm.chunks[it["root_index"]].kind == "PMGI")
    rep.add(A, "index depth / root chunk consistent", exp_depth_ok,
            f"depth {it['depth']}, root {it['root_index']}, {len(pmgi)} PMGI")

    # PMGL chain
    chain, seen, n = [], set(), it["first_pmgl"]
    while 0 <= n < len(chm.chunks) and n not in seen and chm.chunks[n].kind == "PMGL":
        seen.add(n)
        chain.append(chm.chunks[n])
        if n == it["last_pmgl"]:
            break
        n = chm.chunks[n].next
    rep.add(A, "PMGL next-links cover all listing chunks", len(chain) == len(pmgl),
            f"{len(chain)} of {len(pmgl)}")
    prev_ok = all(c.prev == (chain[i - 1].number if i else -1) for i, c in enumerate(chain))
    rep.add(A, "PMGL prev-links consistent", prev_ok, soft=True)
    qr_ok = all(c.quickref_count == c.entry_count for c in pmgl + pmgi)
    rep.add(A, "quickref entry counts match", qr_ok, soft=True)
    names = [nm for c in chain for nm in c.names]
    keys = [nm.encode("utf-8").lower() for nm in names]
    rep.add(A, "entries sorted case-insensitively", all(a < b for a, b in zip(keys, keys[1:])),
            f"{len(names)} entries")
    idx_ok = True
    for c in pmgi:
        for nm, child in zip(c.names, c.children):
            if not (0 <= child < len(chm.chunks)) or not chm.chunks[child].names or \
                    chm.chunks[child].names[0] != nm:
                idx_ok = False
    rep.add(A, "PMGI entries point at the chunk they name", idx_ok if pmgi else None,
            "" if pmgi else "no index chunks (small directory)")
    P.update({"ITSP version": str(it["version"]), "ITSP header length": _hexs(it["header_len"]),
              "Directory chunk size": _hexs(it["chunk_size"]),
              "Quickref density": str(it["density"]),
              "Index depth": f"{it['depth']} ({len(pmgl)} PMGL, {len(pmgi)} PMGI)",
              "Directory sort": "case-insensitive" if all(a < b for a, b in zip(keys, keys[1:])) else "other"})

    # ---- directory contents --------------------------------------------
    A = "Directory entries"
    required = ["::DataSpace/NameList", MSC + "ControlData", MSC + "SpanInfo",
                MSC + "Transform/List", RESET_TABLE, MSC + "Content"]
    missing = [r for r in required if r not in chm.by_name]
    rep.add(A, "DataSpace files present", not missing, ", ".join(missing))
    if missing:
        return rep
    rep.add(A, "only sections 0 and 1 used", all(e.section in (0, 1) for e in chm.entries))
    sec0_len = len(chm.data) - chm.content_off
    rep.add(A, "section 0 entries inside file",
            all(e.offset + e.length <= sec0_len for e in chm.entries if e.section == 0))
    n_files = sum(1 for e in chm.entries if not e.name.endswith("/") and not e.name.startswith("::"))
    rep.add(A, "stored files", None, f"{n_files:,}")

    # ---- DataSpace ------------------------------------------------------
    A = "DataSpace"
    nl = chm.read_section0("::DataSpace/NameList")
    try:
        cnt = struct.unpack_from("<H", nl, 2)[0]
        pos, nl_names = 4, []
        for _ in range(cnt):
            ln = struct.unpack_from("<H", nl, pos)[0]
            nl_names.append(nl[pos + 2:pos + 2 + 2 * ln].decode("utf-16-le"))
            pos += 4 + 2 * ln
    except (struct.error, UnicodeDecodeError):
        nl_names = []
    rep.add(A, "NameList = Uncompressed, MSCompressed", nl_names == ["Uncompressed", "MSCompressed"],
            ", ".join(nl_names))
    tl = chm.read_section0(MSC + "Transform/List").decode("utf-16-le", "replace")
    rep.add(A, "Transform/List names the LZX transform", LZX_GUID.startswith(tl.rstrip("\0")) and tl,
            f"{tl!r} ({len(tl) * 2} bytes)")
    span, = struct.unpack("<Q", chm.read_section0(MSC + "SpanInfo")[:8])
    cd = chm.control_data
    rep.add(A, "ControlData signature 'LZXC'", cd["signature"] == b"LZXC")
    rep.add(A, "ControlData version 2", cd["version"] == 2, str(cd["version"]), soft=cd["version"] == 1)
    win = cd["window_size"]
    rep.add(A, "LZX window 32 KiB..2 MiB, power of two",
            win and not win & (win - 1) and 0x8000 <= win <= 0x200000, _hexs(win))
    rep.add(A, "reset interval multiple of 32 KiB",
            cd["reset_interval"] and cd["reset_interval"] % 0x8000 == 0, _hexs(cd["reset_interval"]))
    rt = chm.reset_table
    rep.add(A, "ResetTable version 2", rt["version"] == 2, str(rt["version"]))
    rep.add(A, "ResetTable entry size 8, header 0x28", rt["entry_size"] == 8 and rt["header_len"] == 0x28)
    rep.add(A, "ResetTable block size 0x8000", rt["block_size"] == 0x8000, _hexs(rt["block_size"]))
    rep.add(A, "uncompressed length = SpanInfo", rt["uncompressed_len"] == span,
            f"{rt['uncompressed_len']:,}")
    content_len = chm.by_name[MSC + "Content"].length
    rep.add(A, "compressed length = Content size", rt["compressed_len"] == content_len,
            f"{rt['compressed_len']:,}")
    frames = (rt["uncompressed_len"] + 0x7FFF) // 0x8000
    rep.add(A, "one ResetTable entry per 32 KiB frame", rt["count"] == frames,
            f"{rt['count']} entries, {frames} frames")
    offs = rt["offsets"]
    rep.add(A, "frame offsets start at 0 and increase",
            (not offs or offs[0] == 0) and all(a < b for a, b in zip(offs, offs[1:])) and
            all(o < content_len for o in offs))
    over = [e.name for e in chm.entries if e.section == 1 and e.offset + e.length > span]
    rep.add(A, "section 1 entries inside uncompressed span", not over, ", ".join(over[:3]))
    P.update({"NameList": ", ".join(nl_names),
              "Transform/List": f"{len(tl) * 2} bytes",
              "LZXC version": str(cd["version"]),
              "LZX window size": _hexs(win),
              "LZX reset interval": _hexs(cd["reset_interval"]),
              "LZX windows per reset": str(cd["windows_per_reset"]),
              "ControlData": cd["raw"].hex(),
              "ResetTable header": f"v{rt['version']}, entry {rt['entry_size']}, "
                                   f"header {_hexs(rt['header_len'])}, block {_hexs(rt['block_size'])}",
              "ResetTable uncompressed length": "actual (not padded)" if rt["uncompressed_len"] % 0x8000 else
              "multiple of 32 KiB"})

    # ---- LZX stream -----------------------------------------------------
    A = "LZX stream"
    if decompress:
        try:
            sec1 = chm.decompress_all()
            ok = len(sec1) == rt["uncompressed_len"]
            rep.add(A, "every frame decompresses", ok, f"{len(sec1):,} bytes from {frames} frames")
        except Exception as exc:  # noqa: BLE001 - report any decoder failure
            rep.add(A, "every frame decompresses", False, f"{type(exc).__name__}: {exc}")
        st = chm.lzx_stats
        blocks = ", ".join(f"{k} {st[k]}" for k in ("verbatim", "aligned", "uncompressed") if st[k])
        rep.add(A, "block types used", None, blocks)
        rep.add(A, "E8 call translation", None, "on" if st["e8-translation"] else "off")
        P["LZX block types"] = ", ".join(k for k in ("verbatim", "aligned", "uncompressed") if st[k])
        P["LZX E8 translation"] = "on" if st["e8-translation"] else "off"
        rep.add(A, "compression ratio", None,
                f"{100.0 * content_len / max(1, rt['uncompressed_len']):.0f}%")

    # ---- HTML Help system files ----------------------------------------
    _check_system(chm, rep, decompress)
    if decompress:
        _check_topics(chm, rep)
        _check_tocidx(chm, rep)
        _check_fts(chm, rep)

    # ---- feature profile -------------------------------------------------
    A = "Features"
    for name, desc in OPTIONAL_FILES:
        e = chm.by_name.get(name)
        present = e is not None
        where = f"section {e.section}, {e.length:,} bytes" if e else "absent"
        rep.add(A, f"{name} ({desc})", None, where)
        P[f"has {name}"] = f"yes (section {e.section})" if present else "no"
    return rep


def _check_system(chm: CHMFile, rep: Report, decompress: bool) -> None:
    A = "#SYSTEM"
    P = rep.params
    if "/#SYSTEM" not in chm.by_name:
        rep.add(A, "#SYSTEM present", False, "the HTML Help viewer requires it")
        return
    e = chm.by_name["/#SYSTEM"]
    if e.section == 1 and not decompress:
        rep.add(A, "#SYSTEM readable", None, "in section 1; skipped with --no-decompress")
        return
    try:
        data = chm.read("/#SYSTEM")
    except Exception as exc:  # noqa: BLE001
        rep.add(A, "#SYSTEM readable", False, str(exc))
        return
    version, = struct.unpack_from("<I", data)
    rep.add(A, "version 2 or 3", version in (2, 3), str(version))
    pos, codes, ok = 4, [], True
    values: Dict[int, bytes] = {}
    while pos < len(data):
        if pos + 4 > len(data):
            ok = False
            break
        code, ln = struct.unpack_from("<HH", data, pos)
        values.setdefault(code, data[pos + 4:pos + 4 + ln])
        codes.append(code)
        pos += 4 + ln
    rep.add(A, "records parse exactly to end of file", ok and pos == len(data))
    rep.add(A, "record codes", None, ", ".join(f"{c} {SYSTEM_CODES.get(c, '?')}" for c in codes))
    if 4 in values:
        v4 = values[4]
        rep.add(A, "code 4 (LCID/flags) is 28 or 36 bytes", len(v4) in (28, 36), f"{len(v4)} bytes")
        if len(v4) >= 20:
            lcid, dbcs, fts, klinks, alinks = struct.unpack_from("<IIIII", v4)
            rep.add(A, "code 4 fields", None,
                    f"LCID {_hexs(lcid)}, DBCS {dbcs}, full-text {fts}, KLinks {klinks}, ALinks {alinks}")
    for code in (0, 1, 2, 3, 5, 6, 9, 16):
        if code in values:
            txt = values[code].split(b"\0")[0].decode("latin-1")
            rep.add(A, SYSTEM_CODES[code], None, txt)
    P.update({"#SYSTEM version": str(version), "#SYSTEM section": str(e.section),
              "#SYSTEM code 4 length": str(len(values.get(4, b""))) if 4 in values else "absent",
              "TOC storage": "binary (#TOCIDX)" if 11 in values or "/#TOCIDX" in chm.by_name else
              ("sitemap .hhc" if 0 in values else "none"),
              "Index storage": _index_storage(chm, values, decompress)})

    # #STRINGS / #WINDOWS
    strings = b""
    if "/#STRINGS" in chm.by_name and decompress:
        strings = chm.read("/#STRINGS")
        rep.add("#STRINGS", "starts with an empty string (NUL)", strings[:1] == b"\0", soft=True)
    if "/#WINDOWS" in chm.by_name and decompress:
        A = "#WINDOWS"
        w = chm.read("/#WINDOWS")
        count, size = struct.unpack_from("<II", w)
        rep.add(A, "entry size 196 (v1.1) or 188 (v1.0)", size in (188, 196), str(size))
        rep.add(A, "length = 8 + count * size", len(w) == 8 + count * size, f"{count} window(s)")
        P["#WINDOWS entry size"] = str(size)
        for i in range(count):
            rec = w[8 + i * size:8 + (i + 1) * size]
            if len(rec) < 0x70:
                break
            str_fields = {"name": 0x08, "caption": 0x14, "toc": 0x60, "index": 0x64,
                          "file": 0x68, "home": 0x6C}
            vals = {}
            bad = []
            for k, off in str_fields.items():
                so, = struct.unpack_from("<I", rec, off)
                if so and so >= len(strings):
                    bad.append(k)
                elif so:
                    vals[k] = strings[so:strings.index(b"\0", so)].decode("latin-1")
            rep.add(A, f"window {i}: string offsets inside #STRINGS", not bad, ", ".join(bad))
            props, = struct.unpack_from("<I", rec, 0x10)
            rep.add(A, f"window {i}", None,
                    f"name={vals.get('name', '')!r} caption={vals.get('caption', '')!r} "
                    f"props={_hexs(props)} toc={vals.get('toc', '')!r} home={vals.get('home', '')!r}")
    if "/#TOPICS" in chm.by_name:
        n = chm.by_name["/#TOPICS"].length
        rep.add("Topic tables", "#TOPICS is a whole number of 16-byte entries", n % 16 == 0,
                f"{n // 16:,} topics")
    if "/#URLTBL" in chm.by_name:
        n = chm.by_name["/#URLTBL"].length
        rep.add("Topic tables", "#URLTBL is a whole number of 12-byte entries (4 KiB blocks)",
                (n % 4096) % 12 == 0 or (n % 4096) % 12 == 4, f"{n:,} bytes", soft=True)


def _index_storage(chm: CHMFile, values: Dict[int, bytes], decompress: bool) -> str:
    """How the keyword index is stored; an index without keywords is "empty"
    whether it is an .hhk or the $WWKeywordLinks placeholders hhc.exe leaves."""
    if "/$WWKeywordLinks/BTree" in chm.by_name:
        return "binary ($WWKeywordLinks)"
    if 1 in values:
        name = "/" + values[1].split(b"\0")[0].decode("latin-1")
        if decompress and name in chm.by_name:
            from . import sitemap
            items = sitemap.parse_sitemap(chm.read(name).decode("utf-8", "replace"))
            if not items:
                return "empty"
        return "sitemap .hhk"
    return "empty" if 7 in values else "none"


def _read_topics(chm: CHMFile):
    """[(toc offset, title offset, #URLTBL offset, flags)] or None."""
    if "/#TOPICS" not in chm.by_name:
        return None
    data = chm.read("/#TOPICS")
    return [struct.unpack_from("<IIIH", data, i) for i in range(0, len(data) - 15, 16)]


def _check_topics(chm: CHMFile, rep: Report) -> None:
    """#URLTBL rows must be sorted on the URL hash: the viewer binary-searches
    them to map the page being shown to its topic (e.g. to sync the TOC)."""
    from .internal import url_hash
    if not all(n in chm.by_name for n in ("/#TOPICS", "/#URLTBL", "/#URLSTR")):
        return
    A = "Topic tables"
    urltbl, urlstr = chm.read("/#URLTBL"), chm.read("/#URLSTR")
    topics = _read_topics(chm) or []
    rows, bad_ref = [], 0
    for block in range(0, len(urltbl), 0x1000):
        for k in range(341):
            off = block + 12 * k
            if off + 12 > len(urltbl):
                break
            key, topic, so = struct.unpack_from("<III", urltbl, off)
            if so + 8 >= len(urlstr) or topic >= len(topics) or topics[topic][2] != off:
                bad_ref += 1
                continue
            end = urlstr.find(b"\0", so + 8)
            rows.append((key, urlstr[so + 8:end if end >= 0 else len(urlstr)]))
    rep.add(A, "#URLTBL rows and #TOPICS entries point at each other", not bad_ref,
            f"{bad_ref} bad" if bad_ref else f"{len(rows):,} rows")
    keys = [k for k, _ in rows]
    rep.add(A, "#URLTBL sorted by URL key", keys == sorted(keys), soft=True)
    wrong = sum(url_hash(u.decode("utf-8", "replace")) != k for k, u in rows)
    rep.add(A, "#URLTBL keys are the URL hashes", not wrong,
            f"{wrong:,} of {len(rows):,} wrong: the viewer can't map pages to topics" if wrong
            else "", soft=True)


def _check_tocidx(chm: CHMFile, rep: Report) -> None:
    from . import tocidx
    if "/#TOCIDX" not in chm.by_name:
        return
    A = "Binary TOC"
    data = chm.read("/#TOCIDX")
    try:
        roots = tocidx.read_tocidx(data)
    except (ValueError, struct.error) as exc:
        rep.add(A, "#TOCIDX node tree decodes", False, str(exc))
        return
    nodes: list = []
    stack = list(roots)
    while stack:
        n = stack.pop()
        nodes.append(n)
        stack.extend(n.children)
    rep.add(A, "#TOCIDX node tree decodes", True, f"{len(nodes):,} entries")
    crossing = [n for n in nodes if n.offset // 0x1000 != (n.offset + (27 if n.flags & 4 else 19)) // 0x1000]
    rep.add(A, "no entry crosses a 4 KiB block", not crossing, f"{len(crossing)} do" if crossing else "")
    topics = _read_topics(chm) or []
    bad = [n for n in nodes if n.has_local and n.ref >= len(topics)]
    rep.add(A, "entries point at existing topics", not bad, f"{len(bad)} don't" if bad else "")
    back = sum(1 for n in nodes if n.has_local and n.ref < len(topics) and topics[n.ref][0] == n.offset)
    firsts = len({n.ref for n in nodes if n.has_local})
    rep.add(A, "their topics point back at the entries", back >= firsts,
            f"{back:,} of {firsts:,} topics", soft=True)
    hdr = struct.unpack_from("<IIII", data, 0)
    rep.add(A, "header: records and topic list inside the file",
            hdr[0] == 0x1000 and hdr[3] <= hdr[1] <= len(data) and hdr[1] + 16 * hdr[2] <= len(data),
            f"{hdr[2]:,} records")
    if "/#IDXHDR" in chm.by_name:
        idx = chm.read("/#IDXHDR")
        rep.add(A, "#IDXHDR signature 'T#SM', 4096 bytes", idx[:4] == b"T#SM" and len(idx) == 4096)


def _check_fts(chm: CHMFile, rep: Report) -> None:
    from . import fts
    if "/$FIftiMain" not in chm.by_name or not chm.by_name["/$FIftiMain"].length:
        return
    A = "Full-text search"
    try:
        r = fts.FtsReader(chm.read("/$FIftiMain"))
    except ValueError as exc:
        rep.add(A, "$FIftiMain header", False, str(exc))
        return
    h = r.header
    rep.add(A, "header: signature, node size 4096, scale 2",
            h.node_size == fts.NODE_SIZE and h.scales == (2, 2, 2) and h.root < len(r.data))
    topics = _read_topics(chm) or []
    try:
        leaves = entries = 0
        prev = None
        order_ok = True
        max_topic = -1
        off = r.first_leaf()
        while off:
            leaves += 1
            off, items = r.leaf_node(off)
            for word, ctx, count, wlc_off, size in items:
                key = (word, ctx)
                order_ok &= prev is None or prev < key
                prev = key
                entries += 1
                hits = r.wlc(count, wlc_off, size)
                if hits:
                    max_topic = max(max_topic, max(hits))
        rep.add(A, "every leaf and word-location list decodes", True,
                f"{entries:,} entries in {leaves} leaves")
        rep.add(A, "leaf count matches header", leaves == h.leaves, f"{leaves} vs {h.leaves}")
        rep.add(A, "words sorted", order_ok)
        rep.add(A, "document numbers are #TOPICS entries", max_topic < len(topics),
                f"highest {max_topic:,}, {len(topics):,} topics")
    except (IndexError, ValueError, struct.error) as exc:
        rep.add(A, "every leaf and word-location list decodes", False, f"{type(exc).__name__}: {exc}")
    rep.add(A, "$OBJINST present", "/$OBJINST" in chm.by_name,
            "" if "/$OBJINST" in chm.by_name else
            "the viewer's Search tab finds nothing without it", soft=True)
    rep.params["Full-text search"] = f"yes, root sizes {h.roots}"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def format_report(rep: Report, show_info: bool = True) -> str:
    out = [f"Verifying {rep.path}", ""]
    area = None
    for c in rep.checks:
        if c.status == INFO and not show_info:
            continue
        if c.area != area:
            area = c.area
            out.append(f"[{area}]")
        detail = f"  ({c.detail})" if c.detail else ""
        mark = {"PASS": "ok  ", "WARN": "WARN", "FAIL": "FAIL", "INFO": "    "}[c.status]
        out.append(f"  {mark} {c.name}{detail}" if c.status != INFO else f"       {c.name}: {c.detail}")
    passed = sum(c.status == PASS for c in rep.checks)
    out.append("")
    out.append(f"Result: {passed} passed, {rep.warnings} warnings, {rep.failures} failed -> "
               f"{'CONFORMS to the CHM format' if not rep.failures else 'DOES NOT CONFORM'}")
    return "\n".join(out)


# Parameters that are part of the file-format "spec" (as opposed to content).
FORMAT_KEYS = [
    "ITSF version", "ITSF header length", "ITSF unknown field", "ITSF GUIDs", "Header section 0",
    "File layout", "ITSP version", "ITSP header length", "Directory chunk size", "Quickref density",
    "Directory sort", "NameList", "Transform/List", "LZXC version", "LZX window size",
    "LZX reset interval", "LZX windows per reset", "ControlData", "ResetTable header",
    "ResetTable uncompressed length", "LZX E8 translation", "#SYSTEM version", "#SYSTEM section",
    "#SYSTEM code 4 length", "#WINDOWS entry size",
]
CONTENT_KEYS = ["Index depth", "LZX block types", "TOC storage", "Index storage"] + \
    [f"has {n}" for n, _ in OPTIONAL_FILES]


def format_comparison(a: Report, b: Report) -> str:
    def table(keys: List[str]) -> Tuple[List[str], int]:
        rows, diffs = [], 0
        w = max(len(k) for k in keys)
        wa = max(len(a.params.get(k, "-")) for k in keys)
        for k in keys:
            va, vb = a.params.get(k, "-"), b.params.get(k, "-")
            same = va == vb
            diffs += not same
            rows.append(f"  {'  ' if same else '≠ '}{k:<{w}}  {va:<{wa}}  {vb}")
        return rows, diffs

    out = ["", "=" * 78, "Side-by-side comparison", f"  A: {a.path}", f"  B: {b.path}", ""]
    rows, d1 = table(FORMAT_KEYS)
    out.append("File-format parameters (must match for the same spec):")
    out += rows
    rows, d2 = table(CONTENT_KEYS)
    out.append("")
    out.append("Content / optional features (may legitimately differ):")
    out += rows
    out.append("")
    out.append(f"{d1} format difference(s), {d2} feature difference(s).")
    return "\n".join(out)
