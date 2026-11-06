#!/usr/bin/env python3
"""
bmvault GUI -- a local web front end for bmvault.

    python3 gui.py --db vault.db --port 8000

Standard library only, no build step, no dependencies; it serves one page and a
small JSON API. Browsing is read-only. Every write (scan, merge, export, split)
is executed by running the actual bmvault CLI as a subprocess, so the GUI cannot
drift away from the command line and its progress output is the real thing.

Binds 127.0.0.1 by default. Use --host 0.0.0.0 to expose it on a network (needed
for a cloud/preview URL); do that only where you trust the network, since anyone
who can reach the port can start a scan or write files.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import bmvault

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "bmvault.py")
ACTIONS = ("scan", "merge", "export", "split")


# ------------------------------------------------------------------- queries

def _connect(db):
    if not os.path.exists(db):
        return None
    return bmvault.connect(db, create=False)


def stats(db):
    conn = _connect(db)
    if conn is None:
        return {"db": os.path.abspath(db), "missing": True, "sources": 0, "rows": 0,
                "unique": 0, "dupes": 0, "dupe_pct": 0.0, "formats": [], "folders": [],
                "fts": False, "errors": 0}
    try:
        rows = conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
        unique = conn.execute("SELECT count(DISTINCT norm_url) FROM bookmarks").fetchone()[0]
        sources = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        errors = conn.execute("SELECT count(*) FROM sources WHERE note IS NOT NULL").fetchone()[0]
        formats = [{"format": f or "?", "files": n, "rows": r} for f, n, r in conn.execute(
            "SELECT coalesce(format,'?'), count(*), coalesce(sum(n_bookmarks),0) "
            "FROM sources GROUP BY 1 ORDER BY 3 DESC")]
        folders = [{"folder": f, "n": n} for f, n in conn.execute(
            "SELECT folder, count(*) c FROM bookmarks WHERE folder IS NOT NULL AND folder != '' "
            "GROUP BY folder ORDER BY c DESC LIMIT 30")]
        fts = bmvault.fts_ready(conn)
    finally:
        conn.close()
    return {
        "db": os.path.abspath(db), "missing": False, "sources": sources, "errors": errors,
        "rows": rows, "unique": unique, "dupes": rows - unique,
        "dupe_pct": (rows - unique) / rows * 100 if rows else 0.0,
        "formats": formats, "folders": folders, "fts": fts,
    }


def search(db, query, limit=50, folder=None):
    """Full-text search, one row per distinct URL, newest metadata intact."""
    query = (query or "").strip()
    folder = (folder or "").strip() or None
    limit = max(1, min(int(limit or 50), 500))
    conn = _connect(db)
    if conn is None:
        return {"results": [], "shown": 0, "truncated": False, "query": query, "folder": folder}
    hits = []
    try:
        copies = ("(SELECT count(*) FROM bookmarks b2 WHERE b2.norm_url = b.norm_url) copies")
        if query:
            terms = re.findall(r"[A-Za-z0-9_]+", query)
            if terms and bmvault.fts_ready(conn):
                expr = " ".join(f'"{t}"*' for t in terms)
                sql = ("SELECT b.url, b.title, b.folder, b.added, b.norm_url, " + copies +
                       " FROM bookmarks_fts f JOIN bookmarks b ON b.id = f.rowid"
                       " WHERE bookmarks_fts MATCH ?" + (" AND b.folder LIKE ?" if folder else "") +
                       " ORDER BY rank LIMIT ?")
                params = [expr] + ([folder + "%"] if folder else []) + [limit * 12]
            else:
                like = f"%{query}%"
                sql = ("SELECT b.url, b.title, b.folder, b.added, b.norm_url, " + copies +
                       " FROM bookmarks b WHERE (b.url LIKE ? OR b.title LIKE ? OR b.folder LIKE ?)" +
                       (" AND b.folder LIKE ?" if folder else "") + " ORDER BY b.title LIMIT ?")
                params = [like, like, like] + ([folder + "%"] if folder else []) + [limit * 12]
        else:
            if not folder:
                return {"results": [], "shown": 0, "truncated": False, "query": query, "folder": folder}
            sql = ("SELECT b.url, b.title, b.folder, b.added, b.norm_url, " + copies +
                   " FROM bookmarks b WHERE b.folder LIKE ? ORDER BY b.title LIMIT ?")
            params = [folder + "%", limit * 12]

        seen = set()
        for url, title, fol, added, norm, n_copies in conn.execute(sql, params):
            if norm in seen:
                continue
            seen.add(norm)
            hits.append({"url": url, "title": title, "folder": fol, "added": added,
                         "copies": n_copies})
            if len(hits) >= limit:
                break
        truncated = len(hits) >= limit
    finally:
        conn.close()
    return {"results": hits, "shown": len(hits), "truncated": truncated,
            "query": query, "folder": folder}


def duplicates(db, limit=25):
    conn = _connect(db)
    if conn is None:
        return {"groups": []}
    limit = max(1, min(int(limit or 25), 200))
    groups = []
    try:
        rows = conn.execute(
            "SELECT norm_url, count(*) c, max(url) FROM bookmarks GROUP BY norm_url "
            "HAVING c > 1 ORDER BY c DESC, norm_url LIMIT ?", (limit,)).fetchall()
        for norm, count, url in rows:
            copies = conn.execute(
                "SELECT b.title, b.folder, b.added, s.path FROM bookmarks b "
                "JOIN sources s ON s.id = b.source_id WHERE b.norm_url = ? ORDER BY b.id LIMIT 20",
                (norm,)).fetchall()
            groups.append({
                "url": url, "count": count,
                "copies": [{"title": t, "folder": f, "added": a, "source": p}
                           for t, f, a, p in copies],
            })
    finally:
        conn.close()
    return {"groups": groups}


def folders(db, limit=400):
    conn = _connect(db)
    if conn is None:
        return {"folders": []}
    try:
        rows = conn.execute(
            "SELECT folder, count(*) c FROM bookmarks WHERE folder IS NOT NULL AND folder != '' "
            "GROUP BY folder ORDER BY c DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    return {"folders": [{"folder": f, "n": n} for f, n in rows]}


def sources(db):
    conn = _connect(db)
    if conn is None:
        return {"sources": []}
    try:
        rows = conn.execute(
            "SELECT path, coalesce(format,'?'), n_bookmarks, note, mtime FROM sources "
            "ORDER BY n_bookmarks DESC LIMIT 500").fetchall()
    finally:
        conn.close()
    return {"sources": [{"path": p, "format": f, "rows": n, "note": note} for p, f, n, note, _m in rows]}


# --------------------------------------------------------------------- jobs

class Jobs:
    """One CLI subprocess at a time, with its output kept for the browser."""

    def __init__(self, workdir):
        self.workdir = workdir
        self.lock = threading.Lock()
        self.job = None

    def current(self):
        with self.lock:
            return self.job or {"status": "idle", "lines": [], "progress": [0, 0]}

    def start(self, argv, action):
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise RuntimeError("a job is already running; wait for it to finish")
            job = {"id": "%08x" % int(time.time() * 1000 % (1 << 32)), "action": action,
                   "argv": argv, "status": "running", "lines": [], "progress": [0, 0],
                   "started": time.time(), "ended": None}
            self.job = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job):
        try:
            # bmvault forces its own output to UTF-8; decode it as UTF-8 even on
            # Windows, where text=True would assume cp1252 and mangle any path
            # that is not ASCII.
            proc = subprocess.Popen(
                job["argv"], cwd=self.workdir, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                with self.lock:
                    job["lines"].append(line)
                    if len(job["lines"]) > 800:
                        del job["lines"][:200]
                    found = re.search(r"\[(\d+)/(\d+)\]", line)
                    if found:
                        job["progress"] = [int(found.group(1)), int(found.group(2))]
            code = proc.wait()
            with self.lock:
                job["status"] = "done" if code == 0 else "failed"
                if code != 0:
                    job["lines"].append(f"exited with code {code}")
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            with self.lock:
                job["lines"].append(f"error: {type(exc).__name__}: {exc}")
                job["status"] = "failed"
        finally:
            with self.lock:
                job["ended"] = time.time()


def build_argv(action, data, db):
    """Translate a UI request into the exact CLI call a human would type."""
    base = [sys.executable, CLI]
    roots = [str(r).strip() for r in data.get("roots", []) if str(r).strip()]

    if action == "scan":
        if not roots:
            raise ValueError("scan needs at least one folder or drive")
        argv = base + ["scan", *roots, "--db", db]
        if data.get("any_name"):
            argv.append("--any-name")
        if data.get("follow"):
            argv.append("--follow")
        return argv

    if action == "merge":
        if not roots:
            raise ValueError("merge needs at least one folder or drive")
        argv = base + ["merge", *roots, "-o", data.get("out") or "all-bookmarks.html", "--db", db]
        if data.get("sort"):
            argv += ["--sort", data["sort"]]
        if data.get("all"):
            argv.append("--all")
        return argv

    if action == "export":
        argv = base + ["export", "-o", data.get("out") or "bookmarks-clean.html",
                       "--db", db, "--format", data.get("format") or "html"]
        if data.get("sort"):
            argv += ["--sort", data["sort"]]
        if data.get("all"):
            argv.append("--all")
        return argv

    if action == "split":
        argv = base + ["split", "--db", db, "--out-dir", data.get("out_dir") or "parts"]
        if data.get("max_entries"):
            argv += ["--max-entries", str(int(float(data["max_entries"])))]
        if data.get("max_size"):
            argv += ["--max-size", str(float(data["max_size"]))]
        if data.get("sort"):
            argv += ["--sort", data["sort"]]
        if data.get("all"):
            argv.append("--all")
        return argv

    raise ValueError(f"unknown action {action!r}")


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    db_path = "vault.db"
    jobs = None

    server_version = "bmvault-gui"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console readable
        pass

    # -- responses
    def _send(self, body, content_type, status=200):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, obj, status=200):
        self._send(json.dumps(obj), "application/json; charset=utf-8", status)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/index.html"):
                return self._send(INDEX_HTML, "text/html; charset=utf-8")
            if parsed.path == "/api/stats":
                return self.send_json(stats(self.db_path))
            if parsed.path == "/api/search":
                return self.send_json(search(
                    self.db_path, qs.get("q", [""])[0],
                    qs.get("limit", ["50"])[0], qs.get("folder", [None])[0]))
            if parsed.path == "/api/duplicates":
                return self.send_json(duplicates(self.db_path, qs.get("limit", ["25"])[0]))
            if parsed.path == "/api/folders":
                return self.send_json(folders(self.db_path))
            if parsed.path == "/api/sources":
                return self.send_json(sources(self.db_path))
            if parsed.path == "/api/job":
                return self.send_json(self.jobs.current())
            self.send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/run":
            return self.send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            action = data.get("action")
            if action not in ACTIONS:
                raise ValueError(f"unknown action {action!r}")
            argv = build_argv(action, data, self.db_path)
            job = self.jobs.start(argv, action)
            return self.send_json(job)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, 400)


def make_handler(db_path, workdir):
    class Bound(Handler):
        pass
    Bound.db_path = db_path
    Bound.jobs = Jobs(workdir)
    return Bound


def main(argv=None):
    # A cp1252 Windows console raises on paths with CJK or emoji in them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Local web GUI for bmvault.")
    parser.add_argument("--db", default="vault.db", help="database to browse (default: vault.db)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; 0.0.0.0 exposes it on the network")
    args = parser.parse_args(argv)

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(args.db, os.getcwd()))
    host, port = httpd.server_address[:2]
    print(f"bmvault GUI on http://{host}:{port}  (db: {os.path.abspath(args.db)})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()
    return 0


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bmvault</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#4493f8;--good:#3fb950}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text)}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between;padding:16px 24px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--good)}
h1{font-size:18px;margin:0;letter-spacing:.4px}
.muted{color:var(--muted);font-size:13px}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 12px;min-width:88px}
.pill b{display:block;font-size:16px}
.pill span{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
nav{display:flex;gap:6px;padding:10px 24px;border-bottom:1px solid var(--line)}
nav button{background:none;border:1px solid transparent;color:var(--muted);padding:7px 14px;border-radius:7px;cursor:pointer;font:inherit;font-size:14px}
nav button.active{background:var(--panel);border-color:var(--line);color:var(--text)}
main{padding:22px 24px;max-width:1080px;margin:0 auto}
.view{display:none}.view.active{display:block}
.row{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
input,select,textarea{background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:9px 11px;font:inherit;width:100%}
input:focus,textarea:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
#q{flex:1;min-width:220px;width:auto}
#limit{width:auto}
.hit{display:block;padding:12px 14px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px;text-decoration:none;color:inherit;background:var(--panel)}
.hit:hover{border-color:var(--accent)}
.hit-title{font-weight:600}
.hit-url{color:var(--accent);font-size:13px;word-break:break-all;margin:2px 0 4px}
.hit-meta{color:var(--muted);font-size:12px}
details{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:8px}
summary{cursor:pointer;word-break:break-all}
.count{color:var(--good);font-weight:600}
.copy{font-size:12px;color:var(--muted);margin-top:6px;padding-left:10px;border-left:2px solid var(--line);word-break:break-all}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:8px}
.card h2{font-size:13px;margin:0 0 2px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
label{font-size:12px;color:var(--muted)}
.check{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)}
.check input{width:auto}
button{background:var(--accent);color:#07101f;border:0;border-radius:8px;padding:9px 14px;font-weight:600;cursor:pointer;font:inherit;font-weight:600}
button.ghost{background:none;border:1px solid var(--line);color:var(--text);font-weight:500}
button:disabled{opacity:.5;cursor:default}
.bar{height:6px;background:#0b0f14;border-radius:99px;overflow:hidden;margin:4px 0 10px}
#bar-fill{height:100%;width:0;background:var(--good);transition:width .3s}
pre#log{background:#0b0f14;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:300px;overflow:auto;font-size:12px;color:#9fb0c0;margin:0;white-space:pre-wrap}
.folder{display:flex;justify-content:space-between;gap:12px;padding:8px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.folder:hover{background:var(--panel)}
.folder .n{color:var(--good)}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span><h1>bmvault</h1><span class="muted" id="dbpath"></span></div>
  <div class="pills" id="pills"></div>
</header>

<nav id="tabs">
  <button data-view="search" class="active">Search</button>
  <button data-view="dupes">Duplicates</button>
  <button data-view="folders">Folders</button>
  <button data-view="run">Run</button>
</nav>

<main>
  <section id="view-search" class="view active">
    <div class="row">
      <input id="q" placeholder="Search titles, URLs and folders — prefix matching, e.g. rust async" autocomplete="off">
      <select id="limit"><option>25</option><option selected>50</option><option>200</option></select>
    </div>
    <div class="row"><span class="muted" id="hint"></span><button class="ghost" id="clear-filter" style="display:none">clear folder filter</button></div>
    <div id="results"></div>
  </section>

  <section id="view-dupes" class="view">
    <div class="row"><span class="muted">Pages saved more than once, worst offenders first.</span>
      <button class="ghost" id="reload-dupes">Refresh</button></div>
    <div id="dupes"></div>
  </section>

  <section id="view-folders" class="view">
    <div class="row"><span class="muted">Click a folder to see what is in it.</span></div>
    <div class="card" style="padding:0" id="folders"></div>
  </section>

  <section id="view-run" class="view">
    <div class="grid">
      <form class="card" data-action="scan">
        <h2>Scan drives</h2>
        <label>Folders or drives, one per line</label>
        <textarea name="roots" rows="3" placeholder="/mnt/old-hdd&#10;D:\\Users\\me"></textarea>
        <label class="check"><input type="checkbox" name="any_name"> read every .html/.csv/.txt, not just bookmark-named files</label>
        <button>Start scan</button>
      </form>

      <form class="card" data-action="merge">
        <h2>Merge to one file</h2>
        <label>Folders or drives</label>
        <textarea name="roots" rows="2" placeholder="/mnt/old-hdd"></textarea>
        <label>Output file</label>
        <input name="out" value="all-bookmarks.html">
        <label>Sort <select name="sort">
          <option value="folder">folder (keep structure)</option>
          <option value="title">title</option><option value="url">url</option><option value="added">date added</option>
        </select></label>
        <button>Merge</button>
      </form>

      <form class="card" data-action="export">
        <h2>Export the database</h2>
        <label>Output file</label>
        <input name="out" value="bookmarks-clean.html">
        <label>Format <select name="format"><option>html</option><option>csv</option><option>jsonl</option></select></label>
        <label>Sort <select name="sort">
          <option value="folder">folder</option><option value="title">title</option>
          <option value="url">url</option><option value="added">date added</option>
        </select></label>
        <label class="check"><input type="checkbox" name="all"> keep duplicate rows</label>
        <button>Export</button>
      </form>

      <form class="card" data-action="split">
        <h2>Split into parts</h2>
        <label>Output folder</label>
        <input name="out_dir" value="parts">
        <label>Entries per file</label>
        <input name="max_entries" type="number" min="1" value="20000">
        <label>…or megabytes per file</label>
        <input name="max_size" type="number" step="0.5" placeholder="e.g. 20">
        <button>Split</button>
      </form>
    </div>

    <div class="card">
      <h2>Job <span class="muted" id="job-status">idle</span></h2>
      <div class="bar"><div id="bar-fill"></div></div>
      <pre id="log">Nothing running.</pre>
    </div>
  </section>
</main>

<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (n) => (n || 0).toLocaleString();
let folderFilter = null;
let pollTimer = null;

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opts);
  const text = await res.text();
  if (!res.ok) throw new Error(text || res.statusText);
  return text ? JSON.parse(text) : {};
}

function show(view) {
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === "view-" + view));
  if (view === "dupes") loadDupes();
  if (view === "folders") loadFolders();
}

document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => show(b.dataset.view)));

async function loadStats() {
  const s = await api("/api/stats");
  $("#dbpath").textContent = s.db;
  const pills = [
    ["files", fmt(s.sources)],
    ["records", fmt(s.rows)],
    ["unique URLs", fmt(s.unique)],
    ["duplicate copies", fmt(s.dupes)],
    ["redundant", (s.dupe_pct || 0).toFixed(1) + "%"],
  ];
  $("#pills").innerHTML = pills.map(([k, v]) =>
    '<div class="pill"><b>' + esc(v) + "</b><span>" + k + "</span></div>").join("");
  if (s.missing) {
    $("#results").innerHTML = '<div class="card"><h2>No database yet</h2>' +
      '<p class="muted">Nothing at ' + esc(s.db) + ". Run a scan from the Run tab, or start the GUI with --db pointing at an existing vault.db.</p></div>";
  }
}

async function loadSearch() {
  const q = $("#q").value.trim();
  const limit = $("#limit").value;
  const params = new URLSearchParams({ q: q, limit: limit });
  if (folderFilter) params.set("folder", folderFilter);
  $("#clear-filter").style.display = folderFilter ? "inline-block" : "none";
  const data = await api("/api/search?" + params.toString());
  const parts = [];
  if (folderFilter) parts.push("folder: " + folderFilter);
  if (q) parts.push((data.truncated ? "first " : "") + data.shown + (data.truncated ? "+" : "") + " result(s)");
  $("#hint").textContent = parts.join("  ·  ");
  $("#results").innerHTML = data.results.map((r) =>
    '<a class="hit" href="' + esc(r.url) + '" target="_blank" rel="noreferrer noopener">' +
      '<div class="hit-title">' + esc(r.title || r.url) + "</div>" +
      '<div class="hit-url">' + esc(r.url) + "</div>" +
      '<div class="hit-meta">' + esc(r.folder || "no folder") +
        (r.added ? " · " + esc(r.added) : "") +
        (r.copies > 1 ? " · saved " + r.copies + "×" : "") + "</div></a>").join("")
    || (q || folderFilter ? '<div class="muted">no matches</div>' : "");
}

let searchTimer = null;
$("#q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadSearch, 200);
});
$("#limit").addEventListener("change", loadSearch);
$("#clear-filter").addEventListener("click", () => { folderFilter = null; loadSearch(); });

async function loadDupes() {
  const data = await api("/api/duplicates?limit=30");
  $("#dupes").innerHTML = data.groups.map((g) =>
    "<details><summary><span class=\\"count\\">" + g.count + "×</span> " + esc(g.url) + "</summary>" +
      g.copies.map((c) =>
        '<div class="copy">' + esc(c.title || "(no title)") +
        " · " + esc(c.folder || "no folder") + (c.added ? " · " + esc(c.added) : "") +
        "<br>" + esc(c.source) + "</div>").join("") + "</details>").join("")
    || '<div class="muted">No duplicates. Either your collection is tidy or nothing has been scanned yet.</div>';
}
$("#reload-dupes").addEventListener("click", loadDupes);

async function loadFolders() {
  const data = await api("/api/folders");
  $("#folders").innerHTML = data.folders.map((f) =>
    '<div class="folder" data-folder="' + esc(f.folder) + '"><span>' + esc(f.folder) +
    '</span><span class="n">' + fmt(f.n) + "</span></div>").join("")
    || '<div class="muted" style="padding:12px">No folders recorded.</div>';
  document.querySelectorAll(".folder").forEach((el) => el.addEventListener("click", () => {
    folderFilter = el.dataset.folder;
    $("#q").value = "";
    show("search");
    loadSearch();
  }));
}

function setJobUI(job) {
  const status = job.status || "idle";
  $("#job-status").textContent = status === "running"
    ? "running · " + job.action + (job.progress && job.progress[1]
        ? " " + job.progress[0] + "/" + job.progress[1] : "")
    : status;
  const pct = job.progress && job.progress[1]
    ? Math.min(100, (job.progress[0] / job.progress[1]) * 100) : (status === "done" ? 100 : 0);
  $("#bar-fill").style.width = pct + "%";
  if (job.lines && job.lines.length) {
    const log = $("#log");
    log.textContent = job.lines.join("\\n");
    log.scrollTop = log.scrollHeight;
  }
  document.querySelectorAll("form.card button").forEach((b) => {
    b.disabled = status === "running";
  });
}

async function pollJob() {
  const job = await api("/api/job");
  setJobUI(job);
  if (job.status === "running") {
    pollTimer = setTimeout(pollJob, 700);
  } else {
    clearTimeout(pollTimer);
    loadStats();
  }
}

document.querySelectorAll("form.card").forEach((form) => {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const data = { action: form.dataset.action };
    const fd = new FormData(form);
    if (form.dataset.action === "scan" || form.dataset.action === "merge") {
      data.roots = String(fd.get("roots") || "").split("\\n").map((s) => s.trim()).filter(Boolean);
    }
    fd.forEach((value, key) => {
      if (key === "roots" || key === "all") return;
      if (value !== "") data[key] = value;
    });
    data["all"] = fd.get("all") === "on";
    $("#log").textContent = "starting…";
    try {
      await api("/api/run", data);
      clearTimeout(pollTimer);
      pollJob();
    } catch (err) {
      $("#log").textContent = String(err.message || err);
    }
  });
});

loadStats();
loadSearch();
pollJob();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
