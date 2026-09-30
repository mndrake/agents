"""Tests for full-text search ($FIftiMain, $OBJINST), the binary TOC
(#TOCIDX, #IDXHDR) and the #URLTBL hash.

Expected values marked "hhc.exe" were read from files Microsoft's compiler
wrote: the Excel 2013 developer documentation and the sample project in
examples/ compiled with HTML Help Workshop 1.3.
"""

import os
import random
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chmc import fts, internal, sitemap, tocidx  # noqa: E402
from chmc.cli import main as cli_main  # noqa: E402
from chmc.project import compile_project, load_folder, load_hhp  # noqa: E402
from chmc.reader import CHMFile  # noqa: E402
from chmc.scaffold import create_project  # noqa: E402
from chmc.verify import verify  # noqa: E402


def quiet(_msg):
    pass


def system_codes(chm):
    data = chm.read("/#SYSTEM")
    pos, out = 4, {}
    while pos < len(data):
        code, ln = struct.unpack_from("<HH", data, pos)
        out[code] = data[pos + 4:pos + 4 + ln]
        pos += 4 + ln
    return out


class CodecTests(unittest.TestCase):
    def test_scale_root_matches_spec_table(self):
        # chmspec, "Scale and root encoding", s=2, r=3
        for value, bits in [(0, "0000"), (7, "0111"), (8, "10000"), (15, "10111"),
                            (16, "1100000"), (31, "1101111"), (32, "111000000"),
                            (64, "11110000000")]:
            code, n = fts.sr_bits(value, 3)
            self.assertEqual(format(code, f"0{n}b"), bits, value)

    def test_scale_root_roundtrip(self):
        rnd = random.Random(3)
        for root in range(0, 8):
            values = [rnd.choice([0, 1, 2, rnd.randrange(1 << rnd.randrange(1, 24))])
                      for _ in range(300)]
            bw = fts.BitWriter()
            for v in values:
                bw.write(*fts.sr_bits(v, root))
            bw.align()
            br = fts.BitReader(bytes(bw.out))
            self.assertEqual([br.sr(root) for _ in values], values)

    def test_encint_le(self):
        for v in (0, 1, 127, 128, 300, 1 << 20, (1 << 32) - 1):
            self.assertEqual(fts.decint_le(fts.encint_le(v), 0), (v, len(fts.encint_le(v))))
        self.assertEqual(fts.encint_le(300), b"\xac\x02")  # low 7 bits first

    def test_url_hash_matches_hhc(self):
        known = {  # hhc.exe
            "XL15Con_offline.hhc": 0x15ED6037,
            "XL15Con_offline.hhk": 0x15ED603F,
            "html/1e9ef356-44e6-480b-bc60-a1263fd2ee90.htm": 0x11A40,
            "index.htm": 419420449,
            "topics/getting_started.htm": 2364980769,
            "toc.hhc": 262941145,
        }
        for url, key in known.items():
            self.assertEqual(internal.url_hash(url), key, url)
        self.assertEqual(internal.url_hash("a/b.htm"), internal.url_hash("a\\b.htm"))


class WordBreakerTests(unittest.TestCase):
    def test_rules_observed_in_hhc_output(self):
        self.assertEqual(fts.split_words("Workbook.Sync Property (Excel)"),
                         ["workbook", "sync", "property", "excel"])
        self.assertEqual(fts.split_words("isn't it's"), ["isnt", "its"])
        self.assertEqual(fts.split_words("DATE(2008,5,23) = 2,147,483,647"),
                         ["date", "2008523", "2147483647"])
        self.assertEqual(fts.split_words("pi is 3.14159. v1.1.234"),
                         ["pi", "is", "3.14159", "v1", "1.234"])
        self.assertEqual(fts.split_words("Café Æsop Straße x_y"),
                         ["cafe", "aesop", "strasse", "x_y"])

    def test_page_words_document_order_and_title_context(self):
        words = fts.page_words("<html><head><title>My Title</title><style>p{}</style>"
                               "<script>var x;</script></head><body><p>Body &amp; text"
                               "</p></body></html>")
        self.assertEqual(words, [("my", 1), ("title", 1), ("body", 0), ("text", 0)])


class FtsIndexTests(unittest.TestCase):
    def build_random(self, topics=60, vocab=6000, seed=5):
        rnd = random.Random(seed)
        words = sorted({"".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789_")
                                for _ in range(rnd.randint(1, 14))) for _ in range(vocab)})
        ix = fts.FtsIndex()
        expected = {}
        for t in range(topics):
            page = [(rnd.choice(words), 1 if i < 3 else 0) for i in range(rnd.randint(5, 400))]
            ix.add_topic(t, page)
            for pos, (w, ctx) in enumerate(page):
                expected.setdefault((w.encode(), ctx), {}).setdefault(t, []).append(pos)
        return ix, expected

    def test_roundtrip_multi_leaf_tree(self):
        ix, expected = self.build_random()
        data = ix.build()
        r = fts.FtsReader(data)
        self.assertGreater(r.header.leaves, 1)
        self.assertGreaterEqual(r.header.depth, 2)
        self.assertEqual(r.header.root, len(data) - fts.NODE_SIZE)
        got = {(e.word, e.context): e.hits for e in r.entries()}
        self.assertEqual(got, expected)
        for (w, _ctx) in list(expected)[:200]:
            want = {}
            for c in (0, 1):
                for t, p in expected.get((w, c), {}).items():
                    want.setdefault(t, []).extend(p)
            self.assertEqual(r.lookup(w.decode()), want)

    def test_word_pairs_never_split_across_leaves(self):
        ix, _ = self.build_random(topics=120, vocab=9000, seed=9)
        r = fts.FtsReader(ix.build())
        off = r.first_leaf()
        while off:
            off, items = r.leaf_node(off)
            self.assertEqual(items[0][1], 0, "a leaf starts with a title entry")

    def test_empty_index(self):
        self.assertEqual(fts.FtsIndex().build(), b"")

    def test_objinst_matches_hhc(self):
        data = fts.build_objinst(1252, 0x409)
        self.assertEqual(len(data), 2751)  # hhc.exe
        self.assertEqual(struct.unpack_from("<IIIIII", data, 0),
                         (0x04000000, 2, 24, 2691, 2715, 36))
        self.assertEqual(struct.unpack_from("<II", data, 24 + 24), (1252, 0x409))
        self.assertEqual(struct.unpack_from("<III", data, 2715 + 16), (666, 1252, 0x409))
        # the 'A' record (class 2 = upper case, sort weight 0x4CE, folds to 'a')
        rec = data[24 + 80 + 10 * 0x41:24 + 80 + 10 * 0x42]
        self.assertEqual(rec, bytes.fromhex("0200ce04614141410000"))


class TocIdxTests(unittest.TestCase):
    def tree(self):
        S = sitemap.SitemapItem
        return [S("Welcome", "index.htm"),
                S("Book without page", "", [S("A", "a.htm"), S("A again", "a.htm"),
                                            S("A anchor", "a.htm#part")]),
                S("Book", "b.htm", [S("Deep", "", [S("C", "c.htm")])])]

    def test_roundtrip(self):
        topics = {"index.htm": 0, "a.htm": 1, "b.htm": 2, "c.htm": 3}
        seen = []
        strings = internal.StringTable("cp1252")

        def topic_for(item, off):
            seen.append((item.local, off))
            return topics.get(item.local.split("#")[0])

        data = tocidx.build_tocidx(self.tree(), topic_for, strings.add)
        roots = tocidx.read_tocidx(data)
        self.assertEqual(len(roots), 3)
        self.assertEqual([n.ref for n in roots[1].children], [1, 1, 1])
        self.assertFalse(roots[1].has_local)
        self.assertEqual(roots[1].ref, strings.index["Book without page"])
        self.assertEqual(roots[1].flags, 0x105)
        self.assertEqual(roots[2].flags, 0x10D)
        self.assertEqual(roots[0].flags, 0x8)
        self.assertEqual(roots[2].children[0].children[0].ref, 3)
        # level order: all top-level entries first
        self.assertEqual([n.offset for n in roots], [0x1000, 0x1014, 0x1030])
        self.assertEqual(len(seen), 6)
        # records: one per entry with a topic, in document order
        hdr = struct.unpack_from("<IIII", data, 0)
        self.assertEqual(hdr[2], 6)
        tl = struct.unpack_from("<6I", data, hdr[3])
        self.assertEqual(tl, (0, 1, 1, 1, 2, 3))
        rec = [struct.unpack_from("<IIII", data, hdr[1] + 16 * i) for i in range(6)]
        self.assertEqual([r[1] for r in rec], list(range(666, 672)))
        self.assertEqual(rec[4][3], 2)  # "Book" covers itself and C

    def test_nodes_do_not_cross_blocks(self):
        S = sitemap.SitemapItem
        items = [S(f"Book {i}", f"b{i}.htm", [S(f"P {i}.{j}", f"p{i}_{j}.htm") for j in range(7)])
                 for i in range(300)]
        data = tocidx.build_tocidx(items, lambda it, off: 0, lambda s: 0)
        roots = tocidx.read_tocidx(data)
        nodes = [n for r in roots for n in [r] + r.children]
        self.assertEqual(len(nodes), 2400)
        for n in nodes:
            size = 28 if n.flags & tocidx.FLAG_BOOK else 20
            self.assertEqual(n.offset // 0x1000, (n.offset + size - 1) // 0x1000)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def build(self, proj, name="out.chm"):
        out = os.path.join(self.tmp, name)
        compile_project(proj, out, log=quiet)
        return out

    def test_hhp_with_search_and_binary_toc(self):
        hhp = create_project(os.path.join(self.tmp, "p"), title="Search Test")
        proj = load_hhp(hhp)
        self.assertTrue(proj.full_text_search)  # the starter project turns it on
        proj.binary_toc = True
        out = self.build(proj)
        rep = verify(out)
        self.assertEqual((rep.failures, rep.warnings), (0, 0),
                         [c for c in rep.checks if c.status in ("FAIL", "WARN")])
        chm = CHMFile(out)
        codes = system_codes(chm)
        self.assertNotIn(0, codes)  # like hhc.exe with a binary TOC
        self.assertEqual(struct.unpack("<I", codes[11])[0], internal.url_hash("toc.hhc"))
        self.assertEqual(codes[13], chm.read("/#IDXHDR"))
        self.assertEqual(struct.unpack_from("<I", codes[4], 8)[0], 1)  # full-text flag
        self.assertEqual(chm.read("/$OBJINST"), fts.build_objinst(1252, 0x409))
        r = fts.FtsReader(chm.read("/$FIftiMain"))
        self.assertTrue(r.lookup("welcome"))
        roots = tocidx.read_tocidx(chm.read("/#TOCIDX"))
        self.assertTrue(roots)

    def test_hhp_defaults_follow_html_help_workshop(self):
        hhp = create_project(os.path.join(self.tmp, "p"), title="T")
        with open(hhp, encoding="utf-8") as fh:
            text = fh.read().replace("Full-text search=Yes\n", "")
        with open(hhp, "w", encoding="utf-8") as fh:
            fh.write(text)
        proj = load_hhp(hhp)
        self.assertFalse(proj.full_text_search)
        self.assertFalse(proj.binary_toc)
        chm = CHMFile(self.build(proj))
        self.assertNotIn("/$FIftiMain", chm.by_name)
        self.assertNotIn("/#TOCIDX", chm.by_name)
        self.assertIn(0, system_codes(chm))

    def test_folder_build_gets_search_tab(self):
        folder = os.path.join(self.tmp, "site")
        os.makedirs(folder)
        for i in range(3):
            with open(os.path.join(folder, f"p{i}.htm"), "w") as fh:
                fh.write(f"<html><title>Page {i}</title><body>word{i} common</body></html>")
        proj = load_folder(folder)
        chm = CHMFile(self.build(proj))
        self.assertIn("/$FIftiMain", chm.by_name)
        props, = struct.unpack_from("<I", chm.read("/#WINDOWS"), 8 + 0x10)
        self.assertTrue(props & internal.HHWIN_PROP_TAB_SEARCH)
        r = fts.FtsReader(chm.read("/$FIftiMain"))
        self.assertEqual(len(r.lookup("common")), 3)

    def test_cli_flags(self):
        hhp = create_project(os.path.join(self.tmp, "p"), title="T")
        out = os.path.join(self.tmp, "cli.chm")
        self.assertEqual(cli_main(["build", hhp, "-o", out, "-q", "--no-search", "--binary-toc"]), 0)
        chm = CHMFile(out)
        self.assertNotIn("/$FIftiMain", chm.by_name)
        self.assertIn("/#TOCIDX", chm.by_name)

    def test_urltbl_sorted_by_hash(self):
        topics = [internal.Topic(f"dir/page{i}.htm", f"P{i}") for i in range(800)]
        tt = internal.build_topics(topics, internal.StringTable("cp1252"))
        keys, seen = [], set()
        for block in range(0, len(tt.urltbl), 0x1000):
            if block + 0x1000 <= len(tt.urltbl):
                self.assertEqual(struct.unpack_from("<I", tt.urltbl, block + 0xFFC)[0], 0x1000)
            for k in range(341):
                off = block + 12 * k
                if off + 12 > len(tt.urltbl):
                    break
                key, topic, _ = struct.unpack_from("<III", tt.urltbl, off)
                keys.append(key)
                seen.add(topic)
                # #TOPICS points at this row
                self.assertEqual(struct.unpack_from("<I", tt.topics, 16 * topic + 8)[0], off)
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(seen, set(range(800)))


if __name__ == "__main__":
    unittest.main()
