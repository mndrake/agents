"""Command line interface: ``python -m chmc <command> ...``."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from .project import ProjectError, compile_project, load_folder, load_hhp
from .reader import CHMFile, CHMFormatError, read_directory
from .scaffold import create_project


def _cmd_build(args) -> int:
    src = args.source
    try:
        if os.path.isdir(src):
            proj = load_folder(src, title=args.title or "", default_topic=args.default_topic or "",
                               make_toc=not args.no_toc, make_index=not args.no_index)
        elif src.lower().endswith(".hhp"):
            proj = load_hhp(src)
            if args.title:
                proj.title = args.title
            if args.default_topic:
                proj.default_topic = args.default_topic
        else:
            print(f"error: {src} is neither a folder nor a .hhp file", file=sys.stderr)
            return 2
        if args.lcid:
            proj.lcid = int(args.lcid, 0)
        if args.search is not None:
            proj.full_text_search = args.search
        if args.binary_toc is not None:
            proj.binary_toc = args.binary_toc

        def log(msg: str) -> None:
            if args.quiet or (msg.startswith("  + ") and not args.verbose):
                return
            print(msg)

        output = args.output
        if output is None and os.path.isdir(src):
            output = os.path.join(os.path.dirname(os.path.abspath(src)), proj.compiled_file)
        res = compile_project(proj, output, level=args.level, log=log)
    except (ProjectError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if res.warnings and args.strict:
        return 3
    return 0


def _cmd_list(args) -> int:
    try:
        entries = read_directory(args.file)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for e in sorted(entries, key=lambda e: e.name.lower()):
        if not args.all and (e.name.startswith("::") or e.name.endswith("/")):
            continue
        print(f"{e.length:>10}  s{e.section}  {e.name}")
    return 0


def _cmd_verify(args) -> int:
    from .verify import format_comparison, format_report, verify
    rep = verify(args.file, decompress=not args.no_decompress)
    print(format_report(rep, show_info=not args.brief))
    status = 1 if rep.failures else 0
    if args.compare:
        ref = verify(args.compare, decompress=not args.no_decompress)
        print()
        print(format_report(ref, show_info=not args.brief))
        print(format_comparison(rep, ref))
        status = status or (1 if ref.failures else 0)
    return status


def _cmd_extract(args) -> int:
    try:
        chm = CHMFile(args.file)
        chm.decompress_all()
    except (OSError, CHMFormatError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    count = 0
    root = os.path.abspath(args.folder)
    for e in chm.entries:
        if not e.name.startswith("/") or e.name.endswith("/"):
            continue
        target = os.path.abspath(os.path.join(root, e.name.lstrip("/")))
        if not target.startswith(root + os.sep):
            print(f"skipping unsafe path {e.name!r}", file=sys.stderr)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(chm.read(e.name))
        count += 1
    print(f"Extracted {count} files to {root}")
    return 0


def _cmd_init(args) -> int:
    try:
        hhp = create_project(args.folder, title=args.title)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created project {hhp}")
    print(f"Build it with:  python -m chmc build {hhp}")
    return 0


def _cmd_gui(args) -> int:
    from .gui import main as gui_main
    gui_main()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="chmc", description="Compile HTML into Microsoft Compiled HTML Help (.chm) files.")
    p.add_argument("--version", action="version", version=f"chmc {__version__}")
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="compile a .hhp project or a folder of HTML files")
    b.add_argument("source", help="HTML Help Workshop project (.hhp) or a folder")
    b.add_argument("-o", "--output", help="output .chm path")
    b.add_argument("-l", "--level", type=int, default=6, choices=range(0, 10), metavar="0-9",
                   help="compression level (0 = store, 9 = best; default 6)")
    b.add_argument("-t", "--title", help="override the help file title")
    b.add_argument("-d", "--default-topic", help="override the default (home) topic")
    b.add_argument("--lcid", help="language id, e.g. 0x409")
    b.add_argument("--no-toc", action="store_true", help="folder mode: don't generate a TOC")
    b.add_argument("--no-index", action="store_true", help="folder mode: don't generate an index")
    b.add_argument("--search", dest="search", action="store_true", default=None,
                   help="build a full-text search index (default: on for folders, "
                        "'Full-text search=' for .hhp projects)")
    b.add_argument("--no-search", dest="search", action="store_false",
                   help="don't build a full-text search index")
    b.add_argument("--binary-toc", dest="binary_toc", action="store_true", default=None,
                   help="also store the contents as a binary TOC (#TOCIDX), like "
                        "'Binary TOC=Yes' in a .hhp project")
    b.add_argument("--no-binary-toc", dest="binary_toc", action="store_false",
                   help="don't build a binary TOC")
    b.add_argument("--strict", action="store_true", help="exit with status 3 on warnings")
    b.add_argument("-v", "--verbose", action="store_true", help="list every file added")
    b.add_argument("-q", "--quiet", action="store_true")
    b.set_defaults(func=_cmd_build)

    ls = sub.add_parser("list", help="list the files stored in a .chm")
    ls.add_argument("file")
    ls.add_argument("-a", "--all", action="store_true", help="include internal entries")
    ls.set_defaults(func=_cmd_list)

    v = sub.add_parser("verify", help="check a .chm against the CHM format specification")
    v.add_argument("file")
    v.add_argument("-c", "--compare", metavar="REF.chm",
                   help="also verify REF.chm and compare format parameters side by side")
    v.add_argument("--no-decompress", action="store_true", help="skip full LZX decompression")
    v.add_argument("-b", "--brief", action="store_true", help="hide informational lines")
    v.set_defaults(func=_cmd_verify)

    x = sub.add_parser("extract", help="decompile a .chm into a folder")
    x.add_argument("file")
    x.add_argument("folder")
    x.set_defaults(func=_cmd_extract)

    i = sub.add_parser("init", help="create a starter help project")
    i.add_argument("folder")
    i.add_argument("-t", "--title", default="My Help File")
    i.set_defaults(func=_cmd_init)

    g = sub.add_parser("gui", help="open the graphical compiler")
    g.set_defaults(func=_cmd_gui)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
