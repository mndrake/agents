"""Tests for chmc.  Run from chm_compiler/:  python -m unittest discover -s tests

Decompression is checked with independent CHM readers when they are
installed (7-Zip's ``7z`` and chmlib's ``extract_chmLib``); those tests are
skipped otherwise.
"""

import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chmc import internal, lzx, sitemap  # noqa: E402
from chmc.cli import main as cli_main  # noqa: E402
from chmc.itsf import ITSFWriter, encint  # noqa: E402
from chmc.project import compile_project, load_folder, load_hhp, parse_window  # noqa: E402
from chmc.reader import CHMFile, read_directory  # noqa: E402
from chmc.verify import format_comparison, format_report, verify  # noqa: E402
from chmc.scaffold import create_project  # noqa: E402

HAVE_7Z = shutil.which("7z") is not None
HAVE_CHMLIB = shutil.which("extract_chmLib") is not None


def quiet(_msg):
    pass


def extract_with(tool, chm, dest):
    if tool == "7z":
        subprocess.run(["7z", "x", "-y", "-o" + dest, chm], check=True, capture_output=True)
    else:
        subprocess.run(["extract_chmLib", chm, dest], check=True, capture_output=True)


def sample_files(n, seed=1):
    rnd = random.Random(seed)
    words = ["alpha", "beta", "gamma", "<p>", "</p>", "help", "topic", "\r\n", "chm"]
    files = {}
    for i in range(n):
        body = " ".join(rnd.choice(words) for _ in range(rnd.randint(0, 4000)))
        data = f"<html><title>Page {i}</title><body>{body}</body></html>".encode()
        if i % 5 == 0:  # incompressible content
            data += bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 50000)))
        files[f"dir{i % 4}/page{i}.htm"] = data
    return files


class UnitTests(unittest.TestCase):
    def test_encint(self):
        self.assertEqual(encint(0), b"\x00")
        self.assertEqual(encint(127), b"\x7f")
        self.assertEqual(encint(128), b"\x81\x00")
        self.assertEqual(encint(0x3FFF), b"\xff\x7f")

    def test_huffman_lengths_are_limited_and_complete(self):
        freqs = [1 << min(i, 30) for i in range(40)]  # very skewed
        lens = lzx.huffman_lengths(freqs, 16)
        self.assertLessEqual(max(lens), 16)
        self.assertAlmostEqual(sum(2.0 ** -l for l in lens if l), 1.0)

    def test_huffman_single_symbol_gets_partner(self):
        lens = lzx.huffman_lengths([0, 5, 0, 0], 16)
        self.assertEqual(sum(1 for l in lens if l), 2)

    def test_lzx_frame_offsets(self):
        data = bytes(range(256)) * 1000  # 256000 bytes -> 8 frames
        res = lzx.compress(data)
        self.assertEqual(len(res.frame_offsets), 8)
        self.assertEqual(res.frame_offsets[0], 0)
        self.assertEqual(res.frame_offsets, sorted(res.frame_offsets))
        self.assertLess(len(res.data), len(data) // 10)

    def test_stored_level_size(self):
        data = os.urandom(70000)
        res = lzx.compress(data, level=0)
        # 3 frames, each 32 KiB + 4 byte header + 12 bytes R0-R2
        self.assertEqual(len(res.data), 3 * (0x8000 + 16))

    def test_window_definition_parsing(self):
        w = parse_window("main", '"My Title","toc.hhc","idx.hhk","a.htm","b.htm",,,,,0x42120,300,0x387e,[10,20,800,600],,,1,,,,0')
        self.assertEqual(w.caption, "My Title")
        self.assertEqual(w.toc, "toc.hhc")
        self.assertEqual(w.home, "b.htm")
        self.assertEqual(w.nav_props, 0x42120)
        self.assertEqual(w.nav_width, 300)
        self.assertEqual(w.rect, [10, 20, 800, 600])
        self.assertEqual(w.show_state, 1)

    def test_windows_record_size(self):
        st = internal.StringTable("cp1252")
        data = internal.build_windows([internal.WindowDef("main", "Title", "toc.hhc")], st)
        self.assertEqual(struct.unpack_from("<II", data), (1, 196))
        self.assertEqual(len(data), 8 + 196)

    def test_sitemap_roundtrip(self):
        items = [sitemap.SitemapItem("A", "a.htm", [sitemap.SitemapItem("A & 1", "a1.htm")]),
                 sitemap.SitemapItem("B", "b.htm")]
        parsed = sitemap.parse_sitemap(sitemap.write_sitemap(items))
        self.assertEqual([i.name for i in parsed], ["A", "B"])
        self.assertEqual(parsed[0].children[0].name, "A & 1")
        self.assertEqual(list(sitemap.iter_locals(parsed)), ["a.htm", "a1.htm", "b.htm"])

    def test_sitemap_sibling_ul_per_child(self):
        # Office 2013 developer docs wrap every child in its own <UL> placed
        # after the parent's </LI>; siblings must not nest under each other.
        def obj(name):
            return f'<LI><OBJECT type="text/sitemap"><param name="Name" value="{name}"></OBJECT></LI>'
        text = ("<UL>" + obj("Root")
                + "<UL>" + obj("A") + "<UL>" + obj("A1") + "</UL></UL>"
                + "<UL>" + obj("B") + "</UL>"
                + "<UL>" + obj("C") + "</UL>"
                + "</UL>")
        root, = sitemap.parse_sitemap(text)
        self.assertEqual([c.name for c in root.children], ["A", "B", "C"])
        self.assertEqual([c.name for c in root.children[0].children], ["A1"])
        self.assertEqual(root.children[1].children, [])

    def test_directory_many_files_uses_index_chunks(self):
        w = ITSFWriter()
        names = [f"/folder/subfolder/some_long_topic_name_{i:05d}.htm" for i in range(3000)]
        for n in names:
            w.add(n, b"x")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "big.chm")
            with open(path, "wb") as fh:
                fh.write(w.build())
            with open(path, "rb") as fh:
                raw = fh.read()
            dir_off, = struct.unpack_from("<Q", raw, 0x48)
            depth, root = struct.unpack_from("<Ii", raw, dir_off + 0x18)
            self.assertGreaterEqual(depth, 2)
            self.assertGreaterEqual(root, 0)
            listed = {e.name for e in read_directory(path)}
            self.assertTrue(set(names) <= listed)

    def test_duplicate_names_rejected(self):
        w = ITSFWriter()
        w.add("/a.htm", b"1")
        with self.assertRaises(ValueError):
            w.add("/A.HTM", b"2")


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hhp_project(self):
        hhp = create_project(os.path.join(self.tmp, "demo"), title="Demo")
        proj = load_hhp(hhp)
        self.assertEqual(proj.title, "Demo")
        self.assertEqual(proj.contents_file, "toc.hhc")
        self.assertEqual(proj.windows[0].name, "main")
        res = compile_project(proj, log=quiet)
        self.assertEqual(res.warnings, [])
        names = {e.name for e in read_directory(res.output)}
        for expected in ("/#SYSTEM", "/#WINDOWS", "/#STRINGS", "/#TOPICS", "/toc.hhc",
                         "/index.hhk", "/index.htm", "/topics/faq.htm",
                         "/styles.css", "/images/logo.png"):  # last two found via links
            self.assertIn(expected, names)

    def test_folder_mode_generates_toc_and_index(self):
        src = os.path.join(self.tmp, "site")
        os.makedirs(os.path.join(src, "guide"))
        with open(os.path.join(src, "index.html"), "w") as fh:
            fh.write("<html><head><title>Home</title></head><body>hi</body></html>")
        with open(os.path.join(src, "guide", "intro.html"), "w") as fh:
            fh.write("<html><head><title>Intro</title></head><body>intro</body></html>")
        proj = load_folder(src)
        self.assertEqual(proj.default_topic, "index.html")
        self.assertEqual(proj.title, "Home")
        toc = sitemap.parse_sitemap(proj.generated["toc.hhc"].decode())
        self.assertEqual(toc[0].name, "Home")
        self.assertEqual(toc[1].children[0].local, "guide/intro.html")
        res = compile_project(proj, os.path.join(self.tmp, "site.chm"), log=quiet)
        names = {e.name for e in read_directory(res.output)}
        self.assertIn("/toc.hhc", names)
        self.assertIn("/index.hhk", names)

    def test_missing_file_warns(self):
        hhp = create_project(os.path.join(self.tmp, "demo"))
        with open(hhp, "a") as fh:
            fh.write("does_not_exist.htm\n")
        res = compile_project(load_hhp(hhp), log=quiet)
        self.assertTrue(any("does_not_exist.htm" in w for w in res.warnings))

    def test_cli_build_and_list(self):
        hhp = create_project(os.path.join(self.tmp, "cli"))
        out = os.path.join(self.tmp, "cli.chm")
        self.assertEqual(cli_main(["build", hhp, "-o", out, "-q"]), 0)
        self.assertTrue(os.path.getsize(out) > 0)
        self.assertEqual(cli_main(["list", out]), 0)


class ReaderAndVerifyTests(unittest.TestCase):
    """Built-in decoder round trips and spec verification (no external tools)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, files, level, name="t.chm"):
        w = ITSFWriter(compression_level=level)
        for n, d in files.items():
            w.add(n, d)
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(w.build())
        return path

    def test_builtin_decoder_roundtrip(self):
        files = sample_files(30, seed=7)
        files["zeros.bin"] = bytes(150000)
        for level in (0, 1, 6):
            chm = CHMFile(self._build(files, level, f"l{level}.chm"))
            for name, data in files.items():
                self.assertEqual(chm.read("/" + name), data, f"{name} level {level}")

    def test_random_access_read_matches_full_decode(self):
        files = sample_files(20, seed=8)
        path = self._build(files, 6)
        single = {n: CHMFile(path).read("/" + n) for n in files}
        full = CHMFile(path)
        full.decompress_all()
        for n in files:
            self.assertEqual(single[n], full.read("/" + n))

    def test_verify_passes_for_compiled_project(self):
        hhp = create_project(os.path.join(self.tmp, "demo"))
        res = compile_project(load_hhp(hhp), log=quiet)
        rep = verify(res.output)
        self.assertEqual(rep.failures, 0, format_report(rep))
        self.assertEqual(rep.warnings, 0, format_report(rep))
        self.assertIn("CONFORMS", format_report(rep))
        self.assertIn("0 format difference(s)", format_comparison(rep, verify(res.output)))

    def test_verify_many_files_with_index_chunks(self):
        files = {f"d/{i:05d}_a_fairly_long_topic_file_name.htm": b"x" * (i % 50) for i in range(2500)}
        files["#SYSTEM"] = internal.build_system(
            title="t", default_topic="", contents_file="", index_file="", default_window="",
            compiled_name="t", lcid=0x409, encoding="cp1252")
        rep = verify(self._build(files, 6))
        self.assertEqual(rep.failures, 0, format_report(rep))
        self.assertIn("PMGI", rep.params["Index depth"])

    def test_verify_detects_corruption(self):
        path = self._build(sample_files(5), 6)
        with open(path, "r+b") as fh:
            fh.seek(0x68)  # file size field in header section 0
            fh.write(struct.pack("<Q", 12345))
        rep = verify(path)
        self.assertGreater(rep.failures, 0)
        self.assertIn("DOES NOT CONFORM", format_report(rep))

    def test_verify_rejects_non_chm(self):
        path = os.path.join(self.tmp, "x.chm")
        with open(path, "wb") as fh:
            fh.write(b"not a chm file at all")
        self.assertGreater(verify(path).failures, 0)

    def test_cli_verify_and_extract(self):
        hhp = create_project(os.path.join(self.tmp, "demo"))
        res = compile_project(load_hhp(hhp), log=quiet)
        self.assertEqual(cli_main(["verify", res.output, "-b"]), 0)
        dest = os.path.join(self.tmp, "out")
        self.assertEqual(cli_main(["extract", res.output, dest]), 0)
        with open(os.path.join(self.tmp, "demo", "topics", "faq.htm"), "rb") as a, \
                open(os.path.join(dest, "topics", "faq.htm"), "rb") as b:
            self.assertEqual(a.read(), b.read())


@unittest.skipUnless(HAVE_7Z or HAVE_CHMLIB, "no external CHM reader installed")
class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _roundtrip(self, files, level):
        w = ITSFWriter(compression_level=level)
        for name, data in files.items():
            w.add(name, data)
        chm = os.path.join(self.tmp, f"t{level}.chm")
        with open(chm, "wb") as fh:
            fh.write(w.build())
        tools = [t for t, ok in (("7z", HAVE_7Z), ("chmlib", HAVE_CHMLIB)) if ok]
        for tool in tools:
            dest = os.path.join(self.tmp, f"{tool}{level}")
            extract_with(tool, chm, dest)
            for name, data in files.items():
                with open(os.path.join(dest, name), "rb") as fh:
                    self.assertEqual(fh.read(), data, f"{tool}: {name} differs (level {level})")

    def test_compressed(self):
        self._roundtrip(sample_files(40), 6)

    def test_stored(self):
        self._roundtrip(sample_files(12, seed=2), 0)

    def test_best(self):
        self._roundtrip(sample_files(12, seed=3), 9)

    def test_edge_sizes(self):
        files = {
            "empty.txt": b"",
            "one.txt": b"x",
            "frame.bin": b"ab" * (lzx.FRAME_SIZE // 2),
            "zeros.bin": bytes(200000),
            "long_run.txt": b"a" * 100000 + b"b" * 3 + b"a" * 5000,
        }
        self._roundtrip(files, 6)

    def test_project_roundtrip(self):
        hhp = create_project(os.path.join(self.tmp, "demo"))
        res = compile_project(load_hhp(hhp), log=quiet)
        dest = os.path.join(self.tmp, "x")
        extract_with("7z" if HAVE_7Z else "chmlib", res.output, dest)
        for rel in ("index.htm", "topics/faq.htm", "styles.css", "images/logo.png"):
            with open(os.path.join(self.tmp, "demo", rel), "rb") as a, \
                    open(os.path.join(dest, rel), "rb") as b:
                self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
