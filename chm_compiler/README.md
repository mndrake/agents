# chmc: CHM compiler

`chmc` compiles HTML into Microsoft Compiled HTML Help (`.chm`) files. It is
written in pure Python using only the standard library, so it runs anywhere
Python does (Windows, macOS, Linux). You don't need Microsoft HTML Help
Workshop or `hhc.exe`.

It includes:

- a **command-line tool** (`python -m chmc build ...`)
- a **desktop GUI** (`python -m chmc gui`, uses Tkinter)
- a **Python API** (`chmc.load_hhp(...)`, `chmc.compile_project(...)`)

## Quick start

```bash
cd chm_compiler

# 1. Create a starter project (an .hhp file, TOC, index, pages, CSS and an image)
python -m chmc init my_help -t "My Product Help"

# 2. Compile it
python -m chmc build my_help/my_help.hhp          # -> my_help/my_help.chm

# ...or compile any folder of HTML files directly
python -m chmc build path/to/site -o site.chm     # TOC and index are generated

# 3. Inspect the result
python -m chmc list my_help/my_help.chm

# Or use the GUI
python -m chmc gui
```

You can also install it to get a `chmc` command: `pip install ./chm_compiler`.

## Input formats

### HTML Help Workshop projects (`.hhp`)

Existing HHW projects compile as they are. Supported sections and keys:

| Section     | Supported                                                                 |
|-------------|---------------------------------------------------------------------------|
| `[OPTIONS]` | `Compiled file`, `Contents file`, `Index file`, `Default topic`, `Title`, `Language`, `Default Window`, `Default Font` |
| `[WINDOWS]` | Full window definitions: caption, TOC/index/home files, navigation pane style and width, toolbar buttons, position, and so on |
| `[FILES]`   | Explicit file list                                                         |

Like `hhc.exe`, chmc follows local links from pages, the TOC and the index
(`href`, `src`, CSS `url(...)`, `@import`, and others). Images, stylesheets and
linked pages are included automatically, even when `[FILES]` doesn't list
them. A missing file produces a warning. Use `--strict` to make warnings fail
the build.

### Plain folders

When you point `build` at a folder, chmc does the following:

- includes every file in the folder, skipping hidden files and the `#`/`$`
  internals of decompiled CHMs
- uses `index.html`, `index.htm`, `default.html` or `default.htm` as the home
  page, or the first page found; override with `-d`
- takes the title from the home page's `<title>`; override with `-t`
- generates a **table of contents** that mirrors the folder tree. A folder's
  `index.html` becomes the link on that folder's node.
- generates a **keyword index** from page titles

If the folder already contains an `.hhc` or `.hhk` file, chmc uses that file
instead of generating one. Use `--no-toc` or `--no-index` to skip generation.

## CLI reference

```
chmc build SOURCE [-o OUT.chm] [-l 0-9] [-t TITLE] [-d TOPIC] [--lcid 0x409]
                  [--no-toc] [--no-index] [--strict] [-v | -q]
chmc list FILE.chm [-a]        # -a also shows internal/DataSpace entries
chmc verify FILE.chm [-c REF.chm] [-b] [--no-decompress]
chmc extract FILE.chm FOLDER   # decompile
chmc init FOLDER [-t TITLE]
chmc gui
```

`-l` sets the compression level: `0` stores data uncompressed (fastest),
`9` searches hardest, and the default is `6`. As a reference, the full PyWin32
documentation (6,854 files, 8.8 MB) compiles in about 8 seconds to 1.6 MB.

## Verifying a CHM against the spec

`chmc verify` checks any `.chm` file, whether chmc built it or another tool
did (Microsoft `hhc.exe`, Excel or Office help files, and so on). It runs
about 50 checks:

- the ITSF and ITSP headers and their GUIDs
- the PMGL/PMGI directory chunks: links, quickref areas, sort order and the
  index tree
- the DataSpace files (NameList, Transform/List, LZXC ControlData, ResetTable
  and SpanInfo)
- a full LZX decompression of every frame, using the built-in decoder, which
  handles verbatim, aligned-offset and uncompressed blocks
- the `#SYSTEM`, `#WINDOWS`, `#STRINGS` and topic tables

The report ends with either `CONFORMS to the CHM format` or
`DOES NOT CONFORM`. The command exits with status 1 when any check fails.

To check that a chmc build uses the same format as a reference CHM, compare
the two:

```bash
python -m chmc build my_help/my_help.hhp
python -m chmc verify my_help/my_help.chm --compare "C:\path\to\VBAXL10.CHM"
```

The comparison is split into two groups:

- **File-format parameters.** These must be identical when two files follow
  the same spec: ITSF/ITSP versions, header lengths, GUIDs, chunk size,
  LZX window and reset interval, ResetTable layout, `#SYSTEM` version and
  the `#WINDOWS` record size.
- **Optional features.** These may legitimately differ, such as a binary TOC,
  a full-text search index or aligned LZX blocks.

Compared with the PyWin32 help file (compiled by Microsoft `hhc.exe`), a chmc
build shows **0 format differences**. The only differences are the optional
features chmc doesn't generate.

The same holds for Microsoft's *Excel 2013 Developer Documentation.chm* (from
the Office 2013 VBA Documentation download; 6,952 files, 9.9 MB, 73 MB
uncompressed). `verify` passes on the original, `extract` output matches
chmlib byte for byte, and recompiling the extracted folder gives a file that
conforms, shows 0 format differences against the original, and decompresses
identically with 7-Zip and chmlib.

## Python API

```python
import chmc

project = chmc.load_hhp("docs/help.hhp")      # or chmc.load_folder("site/")
project.title = "Overridden title"
result = chmc.compile_project(project, "out/help.chm", level=6)
print(result.size, result.file_count, result.warnings)
```

## How it works

A `.chm` file is an ITSF container ("InfoTech Storage Format"):

```
ITSF header ─ header section 0 (file size)
            ─ ITSP directory: PMGL listing chunks + PMGI index chunks (4 KiB each)
            ─ content section 0 (uncompressed): NameList, ControlData, ResetTable,
                                                #SYSTEM, and the compressed stream
            └ content section 1 "MSCompressed": LZX stream of all other files
```

| Module            | Responsibility                                                                 |
|-------------------|--------------------------------------------------------------------------------|
| `chmc/lzx.py`     | LZX compressor: hash-chain LZ77, length-limited canonical Huffman trees, one verbatim block per 32 KiB frame, a reset every 64 KiB (the same parameters as `hhc.exe`) |
| `chmc/itsf.py`    | Container writer: directory chunks with quickref areas, a multi-level PMGI index, the DataSpace files and the ResetTable |
| `chmc/internal.py`| HTML Help system files: `#SYSTEM`, `#WINDOWS` (`HH_WINTYPE` records), `#STRINGS`, `#TOPICS`, `#URLTBL`, `#URLSTR` |
| `chmc/project.py` | `.hhp` parsing, folder projects, link discovery, TOC and index generation, compilation |
| `chmc/sitemap.py` | Reading and writing `.hhc`/`.hhk` sitemap files                                  |
| `chmc/reader.py`  | CHM parser (headers, directory, sections) used by `list`, `extract` and `verify` |
| `chmc/lzxd.py`    | LZX decoder (verbatim, aligned and uncompressed blocks; E8 translation)          |
| `chmc/verify.py`  | Spec conformance checks and side-by-side comparison                            |
| `chmc/gui.py`     | Tkinter front end                                                              |

The container layout, `#SYSTEM` and `#WINDOWS` were checked field by field
against a CHM produced by Microsoft's `hhc.exe`. Compiled files decompress
byte-for-byte with three independent readers: 7-Zip, chmlib
(`extract_chmLib`) and libmspack.

## Limitations

- **No full-text search index** (`$FIftiMain`). The Contents and Index tabs
  work. The Search tab is left off the default window.
- The TOC and index are stored as sitemap files (like `Binary TOC=No` and
  `Binary Index=No` in HHW). Binary TOC and index, `[ALIAS]`/`[MAP]` context
  IDs, and information types are not generated.
- The compressor is pure Python. It is fast enough for typical help projects,
  but very large projects take a few seconds per 10 MB.

## Tests

```bash
cd chm_compiler
python -m unittest discover -s tests
```

When `7z` or `extract_chmLib` is on the `PATH`, the tests also decompress the
output and compare every file byte for byte. On Debian or Ubuntu, install them
with `apt install p7zip-full libchm-bin`.
