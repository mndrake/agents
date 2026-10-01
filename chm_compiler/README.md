# chmc: CHM compiler

`chmc` compiles HTML into Microsoft Compiled HTML Help (`.chm`) files. It is
written in pure Python using only the standard library, so it runs anywhere
Python does (Windows, macOS, Linux). You don't need Microsoft HTML Help
Workshop or `hhc.exe`.

Compiled files have a Contents tab and an Index tab. They can also have a
full-text Search tab and a binary table of contents, the same structures
`hhc.exe` writes.

It includes:

- a **command-line tool** (`python -m chmc build ...`)
- a **desktop GUI** (`python -m chmc gui`, uses Tkinter)
- a **Python API** (`chmc.load_hhp(...)`, `chmc.compile_project(...)`)

## Download

Standalone executables that don't need Python are attached to each
[GitHub release](../../releases): `chmc-windows-x64.exe`, `chmc-macos-arm64`
and `chmc-linux-x64`. Use them like the `chmc` command below, for example
`chmc-windows-x64.exe build my_help\my_help.hhp`. Rename the file to `chmc`
(`chmc.exe`) if you like.

They are built by `.github/workflows/chmc-release.yml`: pushing a tag such as
`v1.0.0` runs the tests on Windows, macOS and Linux, builds each executable
with PyInstaller, smoke-tests it, and publishes the release. To build one
yourself, run `pip install pyinstaller` and then `bash packaging/build.sh`
from `chm_compiler/`; the result is in `dist/`.

The executables aren't code-signed, so Windows SmartScreen and macOS
Gatekeeper may warn before the first run. On macOS, allow it with
`xattr -d com.apple.quarantine chmc-macos-arm64`.

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
| `[OPTIONS]` | `Compiled file`, `Contents file`, `Index file`, `Default topic`, `Title`, `Language`, `Default Window`, `Default Font`, `Full-text search`, `Binary TOC` |
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
- builds a **full-text search index** and shows the Search tab

If the folder already contains an `.hhc` or `.hhk` file, chmc uses that file
instead of generating one. Use `--no-toc` or `--no-index` to skip generation.

## CLI reference

```
chmc build SOURCE [-o OUT.chm] [-l 0-9] [-t TITLE] [-d TOPIC] [--lcid 0x409]
                  [--no-toc] [--no-index] [--search | --no-search]
                  [--binary-toc | --no-binary-toc] [--strict] [-v | -q]
chmc list FILE.chm [-a]        # -a also shows internal/DataSpace entries
chmc verify FILE.chm [-c REF.chm] [-b] [--no-decompress]
chmc extract FILE.chm FOLDER   # decompile
chmc init FOLDER [-t TITLE]
chmc gui
```

`--search` and `--binary-toc` override the project. As in HTML Help Workshop,
a `.hhp` project gets full-text search only with `Full-text search=Yes` and a
binary TOC only with `Binary TOC=Yes`. A folder build gets full-text search by
default and no binary TOC.

`-l` sets the compression level: `0` stores data uncompressed (fastest),
`9` searches hardest, and the default is `6`. As a reference, the full PyWin32
documentation (6,854 files, 8.8 MB) compiles in about 8 seconds to 1.6 MB.

## Verifying a CHM against the spec

`chmc verify` checks any `.chm` file, whether chmc built it or another tool
did (Microsoft `hhc.exe`, Excel or Office help files, and so on). It runs
about 65 checks:

- the ITSF and ITSP headers and their GUIDs
- the PMGL/PMGI directory chunks: links, quickref areas, sort order and the
  index tree
- the DataSpace files (NameList, Transform/List, LZXC ControlData, ResetTable
  and SpanInfo)
- a full LZX decompression of every frame, using the built-in decoder, which
  handles verbatim, aligned-offset and uncompressed blocks
- the `#SYSTEM`, `#WINDOWS`, `#STRINGS` and topic tables, including whether
  the `#URLTBL` keys are the URL hashes the viewer looks pages up by
- the binary TOC (`#TOCIDX`): the entry tree, 4 KiB blocks, and links to and
  from `#TOPICS`
- the full-text index (`$FIftiMain`): every leaf and word-location list, word
  order, document numbers, and the `$OBJINST` file the Search tab needs

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
build shows **0 format differences**.

The same holds for Microsoft's *Excel 2013 Developer Documentation.chm* (from
the Office 2013 VBA Documentation download; 6,952 files, 9.9 MB, 73 MB
uncompressed). `verify` passes on the original, and `extract` output matches
chmlib byte for byte. Recompiling the extracted folder with `--binary-toc`
gives a file that conforms, decompresses identically with 7-Zip and chmlib,
and shows 0 format differences and 1 feature difference against the
original: Microsoft's compressor also uses LZX aligned-offset blocks (in 8
of its 1,154 blocks).

## Full-text search and binary TOC

**Full-text search.** chmc indexes every HTML page into `$FIftiMain`, the
word index behind the viewer's Search tab, and adds the `$OBJINST` file the
Search tab needs. Words follow the rules of `hhc.exe`'s word breaker:
- Letters, digits and `_` make up words.
- `isn't` is indexed as `isnt`.
- Numbers keep their decimal points and lose thousands separators
  (`3.14159`, `2,147,483,647` → `2147483647`).
- Accented Latin letters are folded (`café` → `cafe`).
- Title words are marked, so "search titles only" works.

Pages are decoded as UTF-8 or the project's code page. `hhc.exe` reads UTF-8
pages as ANSI and indexes the mangled bytes, so its index contains junk words
such as `i` (from the byte order mark) that chmc's doesn't.

**Binary TOC.** With `Binary TOC=Yes` (or `--binary-toc`) the contents are
also stored as `#TOCIDX`, with `#IDXHDR`, the way `hhc.exe` stores them. The
`.hhc` file is still included for other viewers.

**Topic table.** `#URLTBL` rows are sorted by the URL hash the viewer uses to
find the topic of the page it shows, e.g. to sync the Contents tab. Earlier
chmc versions wrote zeros there.

### How these were checked

- **Against Microsoft's files.** Decoding the Excel 2013 docs with chmc's
  readers shows:
  - The 6,694-entry `#TOCIDX` matches its `.hhc` entry for entry.
  - chmc's rebuild of it has the same node layout: every offset, flag and
    link is identical.
  - The URL hash reproduces all 6,697 of Microsoft's keys.
  - Feeding Microsoft's word lists to chmc's writer reproduces all 16,459
    index entries, with the same encoding parameters.
- **Against Microsoft's compiler.** HTML Help Workshop's `hhc.exe` (run under
  Wine) compiled `examples/sample_help` with search and binary TOC on. chmc's
  `$FIftiMain` for the same project is byte-identical apart from 4 bytes of
  statistics in its header. `#URLTBL` is identical, and `$OBJINST` is
  identical to the one in both Microsoft files examined (from 1999 and 2013).
- **In Microsoft's viewer.** `hh.exe` with Microsoft's `hhctrl.ocx` (under
  Wine) was used to exercise chmc's builds:
  - The Contents tab displays the binary TOC. A test file whose stored
    `.hhc` had different names confirmed the viewer read `#TOCIDX`.
  - Swapping chmc's index into Microsoft's Excel docs file gives the same
    Search tab result counts as Microsoft's own index (19, 119 and 434 for
    three test words).
  - Opening a result shows the right page and syncs the Contents tab to it.
    With zeroed `#URLTBL` keys the page still opens but the tab doesn't sync.
  - Without `$OBJINST` the Search tab finds nothing, even with a valid index.
- **With chmlib.** pychm's search, which is chmlib's C code, returns the
  expected pages for all 13,476 indexed words of the rebuilt Excel docs.

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
| `chmc/internal.py`| HTML Help system files: `#SYSTEM`, `#WINDOWS` (`HH_WINTYPE` records), `#STRINGS`, `#TOPICS`, `#URLTBL` (hash sorted), `#URLSTR`, `#IDXHDR` |
| `chmc/fts.py`     | Full-text search: word breaker, `$FIftiMain` writer and reader, `$OBJINST`      |
| `chmc/tocidx.py`  | Binary TOC (`#TOCIDX`) writer and reader                                         |
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

- The keyword index is stored as a sitemap file, like `Binary Index=No` in
  HHW; the binary index (`$WWKeywordLinks`) is not generated.
  `[ALIAS]`/`[MAP]` context IDs, information types and merged files aren't
  supported either.
- The search engine's character table in `$OBJINST` is the Western (code
  page 1252) one `hhc.exe` writes. For other code pages, words in plain ASCII
  are found reliably but words with other letters may not be.
- The compressor is pure Python. It is fast enough for typical help projects,
  but very large projects take a few seconds per 10 MB. Building the search
  index adds about 2 seconds per 1,000 pages.

## Tests

```bash
cd chm_compiler
python -m unittest discover -s tests
```

When `7z` or `extract_chmLib` is on the `PATH`, the tests also decompress the
output and compare every file byte for byte. On Debian or Ubuntu, install them
with `apt install p7zip-full libchm-bin`.

## License

MIT; see [LICENSE](LICENSE). `chmc/fts.py` embeds the character table that
Microsoft's `hhc.exe` writes into `$OBJINST`; it is reproduced for
interoperability, as the HTML Help viewer's Search tab needs it.
