"""Command line interface: ``python -m chmc <command> ...``."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from .project import ProjectError, compile_project, load_folder, load_hhp
from .reader import read_directory
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
    b.add_argument("--strict", action="store_true", help="exit with status 3 on warnings")
    b.add_argument("-v", "--verbose", action="store_true", help="list every file added")
    b.add_argument("-q", "--quiet", action="store_true")
    b.set_defaults(func=_cmd_build)

    ls = sub.add_parser("list", help="list the files stored in a .chm")
    ls.add_argument("file")
    ls.add_argument("-a", "--all", action="store_true", help="include internal entries")
    ls.set_defaults(func=_cmd_list)

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
