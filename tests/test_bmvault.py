"""Fixture-driven tests for bmvault: every supported format, plus dedupe rules."""

import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bmvault  # noqa: E402

RUST = "https://en.wikipedia.org/wiki/Rust_(programming_language)"

NETSCAPE = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="1600000000">Bookmarks bar</H3>
    <DL><p>
        <DT><A HREF="https://example.com/a?utm_source=newsletter" ADD_DATE="1600000000">Example A</A>
        <DT><H3>Nested</H3>
        <DL><p>
            <DT><A HREF="%s">Rust language</A>
        </DL><p>
    </DL><p>
    <DT><A HREF="https://example.com/a/">Example A again</A>
</DL><p>
""" % RUST

CHROME = {
    "checksum": "abc",
    "version": 1,
    "roots": {
        "bookmark_bar": {
            "type": "folder",
            "name": "Bookmarks bar",
            "children": [
                {"type": "url", "name": "Python docs",
                 "url": "https://docs.python.org/3/", "date_added": "13200000000000000"},
                {"type": "url", "name": "Rust language", "url": RUST},
            ],
        },
        "other": {"type": "folder", "name": "Other bookmarks", "children": []},
        "synced": {"type": "folder", "name": "Mobile bookmarks", "children": []},
    },
}


def build_firefox(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
        CREATE TABLE moz_bookmarks (
            id INTEGER PRIMARY KEY, parent INTEGER, title TEXT,
            type INTEGER, fk INTEGER, dateAdded INTEGER
        );
    """)
    conn.executemany("INSERT INTO moz_places VALUES (?,?,?)", [
        (10, "https://news.ycombinator.com/", "Hacker News"),
        (11, "https://example.com/a", "Example A"),
    ])
    conn.executemany("INSERT INTO moz_bookmarks VALUES (?,?,?,?,?,?)", [
        (1, 0, "", 2, None, 0),
        (2, 1, "menu", 2, None, 0),
        (3, 2, "Tech", 2, None, 0),
        (20, 3, "Hacker News", 1, 10, 1600000000000000),
        (21, 3, "Example A", 1, 11, 1600000000000000),
    ])
    conn.commit()
    conn.close()


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = cls.tmp.name
        d1 = os.path.join(cls.root, "drive1")
        d2 = os.path.join(cls.root, "drive2")
        os.makedirs(os.path.join(d1, "exports"))
        os.makedirs(os.path.join(d2, "firefox"))

        with open(os.path.join(d1, "exports", "bookmarks.html"), "w", encoding="utf-8") as fh:
            fh.write(NETSCAPE)
        # Chrome writes "Bookmarks" with no file extension.
        with open(os.path.join(d1, "Bookmarks"), "w", encoding="utf-8") as fh:
            json.dump(CHROME, fh)
        build_firefox(os.path.join(d2, "firefox", "places.sqlite"))

        with open(os.path.join(d2, "links.csv"), "w", encoding="utf-8", newline="") as fh:
            fh.write("url,title,folder\n")
            fh.write("https://crates.io/,Crate registry,Dev\n")
            fh.write("https://www.rust-lang.org/,Rust home,Dev\n")

        with open(os.path.join(d2, "bookmarks-notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("https://rust-lang.org/  <- same page, no www\n")
            fh.write("see [Docs](https://docs.python.org/3/)\n")

        with open(os.path.join(d2, "old.url"), "w", encoding="utf-8") as fh:
            fh.write("[InternetShortcut]\nURL=https://sqlite.org/\n")

        # Noise that must be ignored: not a bookmark file, and an unparseable file.
        with open(os.path.join(d2, "shopping-list.csv"), "w", encoding="utf-8") as fh:
            fh.write("item,qty\nmilk,2\n")
        with open(os.path.join(d2, "broken.html"), "w", encoding="utf-8") as fh:
            fh.write("<DL><p><DT><A HREF=")

        cls.d1, cls.d2 = d1, d2
        cls.db = os.path.join(cls.root, "vault.db")
        assert bmvault.main(["scan", d1, d2, "--db", cls.db, "--quiet"]) == 0

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def conn(self):
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    # -- ingest ------------------------------------------------------------
    def test_counts(self):
        conn = self.conn()
        rows = conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
        unique = conn.execute("SELECT count(DISTINCT norm_url) FROM bookmarks").fetchone()[0]
        # 3 html + 2 chrome + 2 firefox + 2 csv + 2 txt + 1 .url
        self.assertEqual(rows, 12)
        self.assertEqual(unique, 7)

    def test_every_format_recognised(self):
        conn = self.conn()
        formats = dict(conn.execute(
            "SELECT format, count(*) FROM sources GROUP BY format"
        ).fetchall())
        self.assertEqual(formats.get("netscape-html"), 1)
        self.assertEqual(formats.get("browser-json"), 1)
        self.assertEqual(formats.get("firefox-sqlite"), 1)
        self.assertEqual(formats.get("csv"), 1)
        self.assertEqual(formats.get("text"), 1)
        self.assertEqual(formats.get("url-shortcut"), 1)

    def test_non_bookmark_files_ignored(self):
        conn = self.conn()
        paths = [r[0] for r in conn.execute("SELECT path FROM sources")]
        self.assertFalse(any("shopping-list" in p for p in paths))

    def test_folder_paths_reconstructed(self):
        conn = self.conn()
        folders = {r[0] for r in conn.execute("SELECT folder FROM bookmarks WHERE folder IS NOT NULL")}
        self.assertIn("Bookmarks bar", folders)
        self.assertIn("Bookmarks bar/Nested", folders)
        self.assertIn("Tech", folders)

    def test_metadata_kept(self):
        conn = self.conn()
        title, added, url = conn.execute(
            "SELECT title, added, url FROM bookmarks WHERE norm_url = 'https://example.com/a' LIMIT 1"
        ).fetchone()
        self.assertEqual(url, "https://example.com/a?utm_source=newsletter")
        self.assertEqual(added, "2020-09-13")

    # -- normalisation ------------------------------------------------------
    def test_tracking_params_dropped(self):
        a = bmvault.normalize_url("https://example.com/a?utm_source=x&utm_medium=y")
        b = bmvault.normalize_url("https://example.com/a")
        self.assertEqual(a[0], b[0])

    def test_meaningful_params_survive_and_sort(self):
        a = bmvault.normalize_url("https://shop.test/item?size=10&color=red")
        b = bmvault.normalize_url("https://shop.test/item?color=red&size=10")
        self.assertEqual(a[0], b[0])
        self.assertIn("size=10", a[0])

    def test_www_and_trailing_slash(self):
        self.assertEqual(
            bmvault.normalize_url("https://www.rust-lang.org/")[0],
            bmvault.normalize_url("https://rust-lang.org")[0],
        )

    def test_fragment_ignored(self):
        self.assertEqual(
            bmvault.normalize_url("https://docs.python.org/3/#intro")[0],
            bmvault.normalize_url("https://docs.python.org/3/")[0],
        )

    def test_bare_host_gets_scheme(self):
        self.assertIsNotNone(bmvault.normalize_url("example.com/page"))

    def test_javascript_and_place_filtered(self):
        for junk in ("javascript:void(0)", "place:type=6", "about:blank", "", "not a url"):
            self.assertIsNone(bmvault.normalize_url(junk), junk)

    def test_scheme_toggle_merges_http(self):
        plain = bmvault.normalize_url("http://example.com/x")[0]
        secure = bmvault.normalize_url("https://example.com/x")[0]
        self.assertNotEqual(plain, secure)
        try:
            bmvault.MERGE_HTTP_HTTPS = True
            self.assertEqual(
                bmvault.normalize_url("http://example.com/x")[0],
                bmvault.normalize_url("https://example.com/x")[0],
            )
        finally:
            bmvault.MERGE_HTTP_HTTPS = False

    # -- export -------------------------------------------------------------
    def test_export_then_reimport_roundtrip(self):
        out = os.path.join(self.root, "merged.html")
        with mock.patch("sys.stdout"):
            bmvault.main(["export", "--format", "html", "-o", out, "--db", self.db])
        self.assertTrue(os.path.getsize(out) > 200)

        second = os.path.join(self.root, "second.db")
        with mock.patch("sys.stderr"):
            bmvault.main(["scan", out, "--db", second, "--quiet"])
        conn = sqlite3.connect(second)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 7)
        self.assertEqual(
            conn.execute("SELECT count(DISTINCT norm_url) FROM bookmarks").fetchone()[0], 7
        )

    def test_export_csv_and_jsonl(self):
        for fmt, name in (("csv", "merged.csv"), ("jsonl", "merged.jsonl")):
            out = os.path.join(self.root, name)
            with mock.patch("sys.stdout"):
                bmvault.main(["export", "--format", fmt, "-o", out, "--db", self.db])
            with open(out, encoding="utf-8") as fh:
                lines = [l for l in fh.read().splitlines() if l.strip()]
            self.assertEqual(len(lines), 8 if fmt == "csv" else 7, fmt)  # csv has a header

    # -- search / report ----------------------------------------------------
    def test_search_uses_fts(self):
        conn = self.conn()
        self.assertTrue(bmvault.fts_ready(conn))
        conn.close()
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            bmvault.main(["search", "rust", "--db", self.db])
        out = buf.getvalue()
        self.assertIn("rust-lang.org", out)
        self.assertIn("wikipedia.org", out)

    def test_search_prefix_match(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            bmvault.main(["search", "pyth", "--db", self.db])
        self.assertIn("docs.python.org", buf.getvalue())

    def test_report_duplicates(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            bmvault.main(["report", "--db", self.db, "--duplicates", "5"])
        out = buf.getvalue()
        self.assertIn("12 bookmark records", out)
        self.assertIn("7 distinct URLs", out)
        self.assertIn("https://example.com/a", out)

    def _export(self, *extra):
        out = os.path.join(self.root, "sorted-out.html")
        with mock.patch("sys.stdout"):
            bmvault.main(["export", "-o", out, "--db", self.db, *extra])
        with open(out, encoding="utf-8") as fh:
            return fh.read()

    def test_export_sorted_by_title(self):
        text = self._export("--sort", "title")
        self.assertNotIn("<H3>", text)  # a global sort is a flat list
        titles = re.findall(r'<DT><A HREF="[^"]+"[^>]*>([^<]*)</A>', text)
        self.assertEqual(len(titles), 7)
        self.assertEqual(titles, sorted(titles, key=str.lower))

    def test_export_sorted_by_url(self):
        text = self._export("--sort", "url")
        urls = re.findall(r'<DT><A HREF="([^"]+)"', text)
        self.assertEqual(len(urls), 7)
        self.assertEqual(urls, sorted(urls, key=str.lower))

    def test_export_sorted_by_date(self):
        text = self._export("--sort", "added")
        self.assertEqual(len(re.findall(r"<DT><A HREF", text)), 7)

    def test_export_all_keeps_every_copy(self):
        text = self._export("--all")
        self.assertEqual(len(re.findall(r"<DT><A HREF", text)), 12)

    def test_merge_writes_one_file_in_one_step(self):
        out = os.path.join(self.root, "one-file.html")
        db = os.path.join(self.root, "merge.db")
        with mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            bmvault.main(["merge", self.d1, self.d2, "-o", out, "--db", db, "--quiet"])
        self.assertTrue(os.path.getsize(out) > 200)

        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 12)

        merged = os.path.join(self.root, "merge-check.db")
        with mock.patch("sys.stderr"):
            bmvault.main(["scan", out, "--db", merged, "--quiet"])
        conn2 = sqlite3.connect(merged)
        self.addCleanup(conn2.close)
        self.assertEqual(conn2.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 7)

    def _scan_into(self, path, db):
        with mock.patch("sys.stderr"):
            bmvault.main(["scan", path, "--db", db, "--quiet"])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        return conn

    def test_split_by_entry_count(self):
        out_dir = os.path.join(self.root, "parts-by-count")
        with mock.patch("sys.stdout"):
            bmvault.main(["split", "--db", self.db, "--out-dir", out_dir, "--max-entries", "3"])
        files = sorted(os.listdir(out_dir))
        self.assertEqual(len(files), 3)  # 7 unique entries, 3 per file
        total = 0
        for i, name in enumerate(files):
            conn = self._scan_into(os.path.join(out_dir, name), os.path.join(self.root, "pc%d.db" % i))
            total += conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
        self.assertEqual(total, 7)

    def test_split_by_file_size(self):
        out_dir = os.path.join(self.root, "parts-by-size")
        with mock.patch("sys.stdout"):
            bmvault.main(["split", "--db", self.db, "--out-dir", out_dir, "--max-size", "0.0004"])
        files = sorted(os.listdir(out_dir))
        self.assertGreater(len(files), 1)
        for name in files:
            self.assertLess(os.path.getsize(os.path.join(out_dir, name)), 2048, name)

    def test_split_a_merged_file_directly(self):
        merged = os.path.join(self.root, "to-split.html")
        with mock.patch("sys.stdout"):
            bmvault.main(["export", "-o", merged, "--db", self.db, "--sort", "title"])
        out_dir = os.path.join(self.root, "parts-from-file")
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            bmvault.main(["split", merged, "--out-dir", out_dir, "--max-entries", "4"])
        files = sorted(os.listdir(out_dir))
        self.assertEqual(len(files), 2)  # 7 entries, 4 per file
        total = 0
        for i, name in enumerate(files):
            conn = self._scan_into(os.path.join(out_dir, name), os.path.join(self.root, "pf%d.db" % i))
            total += conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
        self.assertEqual(total, 7)

    def test_rescan_is_idempotent(self):
        copy_db = os.path.join(self.root, "rescan.db")
        d1 = os.path.join(self.root, "drive1")
        d2 = os.path.join(self.root, "drive2")
        with mock.patch("sys.stderr"):
            bmvault.main(["scan", d1, d2, "--db", copy_db, "--quiet"])
            bmvault.main(["scan", d1, d2, "--db", copy_db, "--quiet"])
        conn = sqlite3.connect(copy_db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 12)


class StreamingTests(unittest.TestCase):
    """The large-file paths: chunked JSON, chunked text, streamed export."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def scan(self, path, db):
        with mock.patch("sys.stderr"):
            bmvault.main(["scan", path, "--db", db, "--quiet"])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        return conn

    def test_large_json_parsed_across_chunk_boundaries(self):
        path = os.path.join(self.root, "bookmarks_large.json")
        entries = [
            {"type": "url", "name": f"Entry {i}", "url": f"https://bulk.example.com/{i}",
             "date_added": "13200000000000000"}
            for i in range(20000)
        ]
        root = {"roots": {"bookmark_bar": {"type": "folder", "name": "Bar", "children": [
            {"type": "folder", "name": "Deep", "children": entries[:10000]},
        ] + entries[10000:]}}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(root, fh)
        self.assertGreater(os.path.getsize(path), 1 << 20)  # several read chunks

        conn = self.scan(path, os.path.join(self.root, "s.db"))
        self.assertEqual(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 20000)
        folders = {r[0] for r in conn.execute("SELECT DISTINCT folder FROM bookmarks")}
        self.assertIn("Bar", folders)
        self.assertIn("Bar/Deep", folders)

    def test_truncated_json_keeps_what_it_read(self):
        path = os.path.join(self.root, "broken-bookmarks.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"roots":{"bar":{"name":"Bar","children":['
                     '{"name":"One","url":"https://a.example/1"},'
                     '{"name":"Two","url":"https://a.example/2"')
        conn = self.scan(path, os.path.join(self.root, "t.db"))
        self.assertEqual(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 1)

    def test_browser_profiles_in_hidden_folders_found(self):
        root = os.path.join(self.root, "disc")
        os.makedirs(os.path.join(root, ".mozilla", "firefox", "abc.default"))
        os.makedirs(os.path.join(root, ".config", "google-chrome", "Default"))
        build_firefox(os.path.join(root, ".mozilla", "firefox", "abc.default", "places.sqlite"))
        with open(os.path.join(root, ".config", "google-chrome", "Default", "Bookmarks"), "w",
                  encoding="utf-8") as fh:
            json.dump(CHROME, fh)
        os.makedirs(os.path.join(root, "Windows", "System32"))
        with open(os.path.join(root, "Windows", "System32", "bookmarks.html"), "w") as fh:
            fh.write(NETSCAPE)

        conn = self.scan(root, os.path.join(self.root, "hidden.db"))
        # 2 from Firefox + 2 from Chrome; the OS folder must be skipped entirely.
        self.assertEqual(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 4)
        paths = [r[0] for r in conn.execute("SELECT path FROM sources")]
        self.assertFalse(any("Windows" in p for p in paths))

    def test_the_database_is_never_ingested_as_a_bookmark_file(self):
        root = os.path.join(self.root, "disc-with-db")
        os.makedirs(root)
        with open(os.path.join(root, "bookmarks.html"), "w", encoding="utf-8") as fh:
            fh.write(NETSCAPE)
        db = os.path.join(root, "vault.db")
        with mock.patch("sys.stderr"):
            bmvault.main(["scan", root, "--db", db, "--quiet"])
            bmvault.main(["scan", root, "--db", db, "--quiet"])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT count(*) FROM sources").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 3)

    def test_single_huge_line_text_dump(self):
        path = os.path.join(self.root, "links-huge.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(" ".join(f"https://oneline.example/{i}" for i in range(40000)) + "\n")
        self.assertGreater(os.path.getsize(path), 1 << 20)
        conn = self.scan(path, os.path.join(self.root, "x.db"))
        self.assertEqual(conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 40000)

    def test_streamed_export_keeps_folder_nesting(self):
        src = os.path.join(self.root, "nested.html")
        pages = [("A", "https://a.example/1"), ("A/B", "https://a.example/2"),
                 ("A/B/C", "https://a.example/3"), ("A/D", "https://a.example/4"),
                 ("A-other", "https://a.example/5")]
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("<DL><p>\n")
            for folder, url in pages:
                for part in folder.split("/"):
                    fh.write(f"<DT><H3>{part}</H3><DL><p>\n")
                fh.write(f'<DT><A HREF="{url}">{url}</A>\n')
                for _ in folder.split("/"):
                    fh.write("</DL><p>\n")
            fh.write("</DL><p>\n")

        db = os.path.join(self.root, "nest.db")
        conn = self.scan(src, db)
        before = {r[0] for r in conn.execute("SELECT folder FROM bookmarks")}

        out = os.path.join(self.root, "nested-out.html")
        with mock.patch("sys.stdout"):
            bmvault.main(["export", "--format", "html", "-o", out, "--db", db])
        conn2 = self.scan(out, os.path.join(self.root, "nest2.db"))
        after = {r[0] for r in conn2.execute("SELECT folder FROM bookmarks")}
        self.assertEqual(before, after)
        self.assertIn("A/B/C", after)
        self.assertEqual(conn2.execute("SELECT count(*) FROM bookmarks").fetchone()[0], 5)


class WindowsCompatTests(unittest.TestCase):
    """Windows-only hazards, asserted in a way that means something on Linux too."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def scan_root(self, root):
        db = os.path.join(self.root, "win.db")
        with mock.patch("sys.stderr"):
            bmvault.main(["scan", root, "--db", db, "--quiet"])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        return conn

    def test_bare_drive_letter_means_the_drive_root(self):
        # "D:" on a Windows command line is the current directory on D:, so a
        # whole-drive scan has to add the separator; "D:\\" is already the root.
        self.assertEqual(bmvault._expand_root("D:"), "D:" + os.sep)
        self.assertEqual(bmvault._expand_root("D:\\"), "D:\\")
        self.assertEqual(bmvault._expand_root(self.root), self.root)

    def test_a_folder_called_dev_is_not_the_linux_device_tree(self):
        os.makedirs(os.path.join(self.root, "dev"))
        for path in (os.path.join(self.root, "bookmarks.html"),
                     os.path.join(self.root, "dev", "bookmarks.html")):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(NETSCAPE)
        conn = self.scan_root(self.root)
        self.assertEqual(conn.execute("SELECT count(*) FROM sources").fetchone()[0], 2)

    def test_legacy_windows_junction_folders_are_skipped(self):
        os.makedirs(os.path.join(self.root, "Application Data"))
        with open(os.path.join(self.root, "Application Data", "bookmarks.html"), "w",
                  encoding="utf-8") as fh:
            fh.write(NETSCAPE)
        with open(os.path.join(self.root, "bookmarks.html"), "w", encoding="utf-8") as fh:
            fh.write(NETSCAPE)
        conn = self.scan_root(self.root)
        paths = [r[0] for r in conn.execute("SELECT path FROM sources")]
        self.assertEqual(len(paths), 1)
        self.assertFalse(any("Application Data" in p for p in paths))

    def test_utf16_url_shortcut_is_read(self):
        # Newer Windows tools write .url as UTF-16; read as UTF-8 the URL= line
        # is separated by NULs and the shortcut silently yields nothing.
        path = os.path.join(self.root, "Old Page.url")
        body = "[InternetShortcut]\r\nURL=https://utf16.example/page\r\n"
        with open(path, "wb") as fh:
            fh.write(body.encode("utf-16"))
        conn = self.scan_root(path)
        row = conn.execute("SELECT url, title FROM bookmarks").fetchone()
        self.assertEqual(row[0], "https://utf16.example/page")

    def test_sqlite_uri_has_the_absolute_path_form(self):
        quoted = bmvault.quote_path(os.path.join(os.sep, "tmp", "a b.places"))
        self.assertTrue(quoted.startswith("/"))
        self.assertIn("%20", quoted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
