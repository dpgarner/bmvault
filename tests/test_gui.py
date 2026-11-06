"""HTTP-level tests for the bmvault GUI: real server, real requests."""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bmvault  # noqa: E402
import gui  # noqa: E402

NETSCAPE = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><H3>Dev</H3>
  <DL><p>
    <DT><A HREF="https://example.com/rust-async?utm_source=x">Async Rust in practice</A>
    <DT><A HREF="https://example.com/rust-async/">Async Rust again</A>
    <DT><A HREF="https://postgres.example/internals">Postgres internals</A>
  </DL><p>
</DL><p>
"""


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        with open(os.path.join(root, "bookmarks.html"), "w", encoding="utf-8") as fh:
            fh.write(NETSCAPE)
        cls.db = os.path.join(root, "vault.db")
        bmvault.main(["scan", root, "--db", cls.db, "--quiet"])

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), gui.make_handler(cls.db, root))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get_json(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as res:
            return json.loads(res.read().decode("utf-8"))

    def post(self, path, payload):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read().decode("utf-8"))

    def test_index_serves_html(self):
        with urllib.request.urlopen(self.url("/"), timeout=10) as res:
            body = res.read().decode("utf-8")
        self.assertIn("<title>bmvault</title>", body)
        self.assertIn("/api/stats", body)

    def test_stats_counts(self):
        data = self.get_json("/api/stats")
        self.assertEqual(data["rows"], 3)
        self.assertEqual(data["unique"], 2)  # the utm variant and the trailing slash collapse
        self.assertEqual(data["dupes"], 1)
        self.assertEqual(data["sources"], 1)

    def test_search_returns_deduped_hits(self):
        data = self.get_json("/api/search?q=rust")
        self.assertEqual(data["shown"], 1)
        hit = data["results"][0]
        self.assertIn("example.com/rust-async", hit["url"])
        self.assertEqual(hit["copies"], 2)

    def test_search_prefix_and_folder_filter(self):
        self.assertEqual(self.get_json("/api/search?q=postg")["shown"], 1)
        self.assertEqual(self.get_json("/api/search?folder=Dev")["shown"], 2)
        self.assertEqual(self.get_json("/api/search?folder=Nope")["shown"], 0)

    def test_duplicates_groups_and_sources(self):
        data = self.get_json("/api/duplicates?limit=5")
        self.assertEqual(len(data["groups"]), 1)
        group = data["groups"][0]
        self.assertEqual(group["count"], 2)
        self.assertTrue(group["copies"][0]["source"].endswith("bookmarks.html"))

    def test_folders_listed(self):
        data = self.get_json("/api/folders")
        self.assertEqual(data["folders"][0]["folder"], "Dev")
        self.assertEqual(data["folders"][0]["n"], 3)

    def test_run_rejects_unknown_action(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/run", {"action": "delete-everything"})
        self.assertEqual(ctx.exception.code, 400)

    def test_run_requires_roots(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/run", {"action": "scan", "roots": []})
        self.assertEqual(ctx.exception.code, 400)

    def test_build_argv_matches_the_cli(self):
        self.assertEqual(
            gui.build_argv("scan", {"roots": ["/mnt/a", "D:/b"], "any_name": True}, "v.db")[2:],
            ["scan", "/mnt/a", "D:/b", "--db", "v.db", "--any-name"])
        self.assertEqual(
            gui.build_argv("split", {"out_dir": "parts", "max_entries": "500"}, "v.db")[2:],
            ["split", "--db", "v.db", "--out-dir", "parts", "--max-entries", "500"])
        with self.assertRaises(ValueError):
            gui.build_argv("nope", {}, "v.db")


if __name__ == "__main__":
    unittest.main(verbosity=2)
