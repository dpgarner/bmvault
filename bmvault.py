#!/usr/bin/env python3
"""
bmvault -- consolidate, de-duplicate and search bookmark files from many drives.

Standard library only, and deliberately one file: copy it to a USB stick, a NAS
or any machine that has Python 3.8+ and it runs. No install step, no venv.

  python3 bmvault.py scan /mnt/drive1 /mnt/drive2 "D:/backup" --db vault.db
  python3 bmvault.py report --db vault.db --duplicates 15
  python3 bmvault.py search "rust async" --db vault.db
  python3 bmvault.py export --format html -o merged.html --db vault.db

What it understands:
  * Netscape bookmark HTML (the export format of every browser, and .htm files)
  * Chrome / Edge / Brave / Chromium JSON ("Bookmarks", with or without extension)
  * Firefox places.sqlite
  * CSV / TSV with a url-ish column
  * plain text or Markdown lists of links
  * Windows Internet Shortcut files (.url)

Every row keeps its source file, folder path, title and added date, so nothing
is flattened beyond recovery.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

VERSION = "0.1.0"
BATCH = 2000

# --------------------------------------------------------------------- URLs

# Query parameters that identify the referrer or the campaign, never the page.
TRACKING_EXACT = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid",
    "igshid", "si", "spm", "mc_cid", "mc_eid", "ref", "ref_src", "referrer",
    "_ga", "_gl", "vero_id", "vero_conv", "s_kwcid", "oly_anon_id", "oly_enc_id",
    "ck_subscriber_id", "trk", "trkcampaign", "sc_cid", "_hsenc", "_hsmi",
}
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "at_", "wt_", "piwik_", "matomo_", "ns_")

SCHEMES_INDEXED = {
    "http", "https", "ftp", "ftps", "sftp", "mailto", "magnet", "irc", "ircs",
    "news", "gopher", "gemini",
}
SCHEMES_IGNORED = {
    "javascript", "data", "about", "chrome", "place", "file", "blob",
    "view-source", "edge", "resource", "wyciwyg",
}

_HEX = re.compile(r"%([0-9a-fA-F]{2})")
_BARE_HOST = re.compile(
    r"^(?:[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\.)+[A-Za-z]{2,}(?::\d+)?(?:[/?#]|$)"
)
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")

# When true, http://x and https://x collapse to a single key. Off by default:
# they can genuinely be different pages, and silent merges lose information.
MERGE_HTTP_HTTPS = False


def _norm_escapes(s: str) -> str:
    """Uppercase percent escapes so %7e and %7E compare equal."""
    return _HEX.sub(lambda m: "%" + m.group(1).upper(), s)


def _clean_raw(raw: str) -> str:
    s = html.unescape(raw or "").strip()
    # Stray quotes and angle brackets arrive from CSV and hand-written lists.
    return s.strip().strip("\"'<>").strip()


def normalize_url(raw):
    """Return (dedupe_key, display_url), or None if this is not a bookmarkable URL.

    The key drops the fragment, tracking parameters, default ports, a leading
    "www." and trailing slashes, and sorts the surviving query parameters. The
    display URL is what the collector actually saved, so nothing is destroyed.
    """
    s = _clean_raw(raw)
    if not s:
        return None

    m = _SCHEME.match(s)
    scheme = m.group(1).lower() if m else None
    if scheme in SCHEMES_IGNORED:
        return None

    if scheme is None:
        # "example.com/page" and "www.example.com" are common in pasted lists.
        if _BARE_HOST.match(s) or s.lower().startswith("www."):
            s = "https://" + s
            scheme = "https"
        else:
            return None

    if "://" not in s:
        # mailto:, magnet:, news: -- no host to canonicalise.
        if scheme in SCHEMES_INDEXED:
            return _norm_escapes(s.lower()), s
        return None

    key = _http_key(s, scheme)
    return (key, s) if key else None


def _http_key(url: str, scheme: str):
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    scheme = (parts.scheme or scheme).lower()
    if scheme not in SCHEMES_INDEXED:
        scheme = "https"
    if MERGE_HTTP_HTTPS and scheme == "http":
        scheme = "https"

    netloc = parts.netloc
    if not netloc:
        return None
    userinfo = ""
    if "@" in netloc:
        userinfo, netloc = netloc.rsplit("@", 1)
        userinfo += "@"
    host = netloc.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]

    try:
        port = parts.port
    except ValueError:
        port = None
    if port in (80, 443) and ((port == 80 and scheme == "http") or (port == 443 and scheme == "https")):
        port = None

    if not host:
        return None

    path = _norm_escapes(parts.path or "")
    if path == "/":
        path = ""  # https://host/ and https://host are the same page
    elif len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        pairs = []
    kept = []
    for k, v in pairs:
        kl = k.lower()
        if kl in TRACKING_EXACT or kl.startswith(TRACKING_PREFIXES):
            continue
        kept.append((k, v))
    kept.sort()
    query = urlencode(kept, doseq=True) if kept else ""

    return urlunsplit((scheme, f"{userinfo}{host}" + (f":{port}" if port else ""), path, query, ""))


# --------------------------------------------------------------------- dates

def _iso(epoch_seconds):
    if epoch_seconds is None:
        return None
    try:
        n = float(epoch_seconds)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def date_unix(value):
    """Netscape ADD_DATE, CSV and .url files: seconds since the Unix epoch."""
    return _iso(value)


def date_firefox(value):
    """Firefox moz_bookmarks.dateAdded: microseconds since the Unix epoch."""
    try:
        return _iso(int(value) / 1_000_000)
    except (TypeError, ValueError):
        return None


def date_chrome(value):
    """Chrome date_added: microseconds since 1601-01-01."""
    try:
        return _iso(int(value) / 1_000_000 - 11644473600)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- parsing

class Sink:
    """Where parsers deliver bookmarks. Real implementation lives in Store."""

    def add(self, url, title=None, folder=None, added=None):
        raise NotImplementedError


def _text(value, limit=500):
    if not value:
        return None
    s = " ".join(str(value).split())
    return s[:limit] or None


class NetscapeParser(HTMLParser):
    """Streaming parser for Netscape bookmark HTML, with folder reconstruction.

    The format nests folders as <DT><H3>name</H3> followed by <DL>...</DL>, so a
    folder name only takes effect once its list opens.
    """

    def __init__(self, sink: Sink):
        super().__init__(convert_charrefs=True)
        self.sink = sink
        self.stack = []
        self.pending_folder = None
        self.in_h3 = False
        self.h3_parts = []
        self.href = None
        self.added = None
        self.link_parts = []

    # -- folders
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h3":
            self.in_h3 = True
            self.h3_parts = []
        elif tag == "dl":
            self.stack.append(self.pending_folder or "")
            self.pending_folder = None
        elif tag == "a" and "href" in a:
            self.href = a.get("href")
            self.added = date_unix(a.get("add_date") or a.get("last_visit"))
            self.link_parts = []

    def handle_endtag(self, tag):
        if tag == "h3":
            self.in_h3 = False
            self.pending_folder = _text("".join(self.h3_parts), 300) or ""
        elif tag == "dl":
            if self.stack:
                self.stack.pop()
        elif tag == "a" and self.href is not None:
            folder = "/".join([p for p in self.stack if p])
            self.sink.add(self.href, "".join(self.link_parts), folder, self.added)
            self.href = None
            self.added = None
            self.link_parts = []

    def handle_data(self, data):
        if self.in_h3:
            self.h3_parts.append(data)
        elif self.href is not None:
            self.link_parts.append(data)


URL_KEYS = ("url", "uri", "href", "link", "website", "address", "weburl")
TITLE_KEYS = ("title", "name", "text", "label", "description")
CONTAINER_KEYS = ("children", "bookmarks", "items", "nodes", "entries")
CHROME_ROOTS = {"roots", "bookmark_bar", "other", "synced", "menu", "mobile", "toolbar"}


def _walk_json(node, folder, emit):
    """Handle Chrome's tree and, as a fallback, any list of objects with a URL."""
    if isinstance(node, list):
        for item in node:
            _walk_json(item, folder, emit)
        return
    if not isinstance(node, dict):
        return

    url = next((node[k] for k in URL_KEYS if isinstance(node.get(k), str) and node.get(k).strip()), None)
    name = _text(next((node[k] for k in TITLE_KEYS if isinstance(node.get(k), str)), None), 300)

    if url:
        added = date_chrome(node.get("date_added") or node.get("dateAdded"))
        emit(url, name, folder, added)
        return

    kids = next((node[k] for k in CONTAINER_KEYS if isinstance(node.get(k), list)), None)
    if kids is not None:
        if name and name.lower() not in CHROME_ROOTS:
            folder = f"{folder}/{name}" if folder else name
        _walk_json(kids, folder, emit)
        return

    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_json(value, folder, emit)


def _open_places(db_path: str):
    """Open a Firefox database read-only and leave the original untouched.

    Two things go wrong on a live profile. A running browser can hold a lock
    that Windows refuses outright, and with a hot WAL the newest bookmarks live
    in places.sqlite-wal, which an "immutable" read deliberately ignores. Both
    are solved the same way: copy the file (plus its WAL) to a scratch folder
    and read the copy. Returns (connection, scratch_dir_to_delete).
    """
    wal = db_path + "-wal"
    hot = os.path.exists(wal) and os.path.getsize(wal) > 0
    if not hot:
        try:
            conn = sqlite3.connect("file://%s?mode=ro&immutable=1" % quote_path(db_path), uri=True)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn, None
        except sqlite3.DatabaseError as exc:
            # Locked or unreadable -> retry on a copy. Not a database at all ->
            # let the caller record the error instead of copying 500 MB of junk.
            if "not a database" in str(exc).lower():
                raise

    scratch = tempfile.mkdtemp(prefix="bmvault-places-")
    copy = os.path.join(scratch, os.path.basename(db_path) or "places.sqlite")
    try:
        shutil.copy2(db_path, copy)
        for suffix in ("-wal", "-shm"):
            side = db_path + suffix
            if os.path.exists(side):
                shutil.copy2(side, copy + suffix)
        return sqlite3.connect(copy), scratch
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def _parse_firefox(db_path: str, sink: Sink):
    conn, scratch = _open_places(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "moz_bookmarks" not in tables or "moz_places" not in tables:
            return 0
        rows = conn.execute(
            "SELECT b.id, b.parent, b.title, b.fk, b.dateAdded, p.url "
            "FROM moz_bookmarks b JOIN moz_places p ON p.id = b.fk "
            "WHERE b.type = 1 AND p.url IS NOT NULL"
        ).fetchall()
        parents = dict(conn.execute("SELECT id, parent FROM moz_bookmarks").fetchall())
        titles = dict(conn.execute("SELECT id, title FROM moz_bookmarks").fetchall())
    finally:
        conn.close()
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    cache = {}

    def folder_of(node_id):
        if node_id in cache:
            return cache[node_id]
        parts, seen, cur = [], set(), node_id
        while cur and cur not in seen:
            seen.add(cur)
            if titles.get(cur) and titles.get(cur) not in ("root", "menu", "toolbar", "unfiled", "mobile"):
                parts.append(titles[cur])
            cur = parents.get(cur)
        path = "/".join(reversed(parts))
        cache[node_id] = path
        return path

    for _id, parent, title, _fk, added, url in rows:
        sink.add(url, title, folder_of(parent), date_firefox(added))
    return len(rows)


def quote_path(path: str) -> str:
    """Absolute path as a URI path: /home/x on POSIX, /C:/x on Windows.

    The leading slash matters: file:C:/x parses as a URI whose host is "C",
    while file:///C:/x parses as a path.
    """
    from urllib.parse import quote
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return quote(p, safe="/:")


def _parse_csv(path: str, sink: Sink):
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        try:
            header = next(reader)
        except StopIteration:
            return 0

        lowered = [h.strip().lower().replace(" ", "") for h in header]
        url_col = next((i for i, h in enumerate(lowered) if h in URL_KEYS or "url" in h or "link" in h), None)
        title_col = next((i for i, h in enumerate(lowered) if h in TITLE_KEYS), None)
        folder_col = next((i for i, h in enumerate(lowered) if "folder" in h or "categor" in h or "tag" in h), None)
        date_col = next((i for i, h in enumerate(lowered) if "date" in h or "added" in h or "created" in h), None)

        rows = []
        if url_col is not None:
            for row in reader:
                if url_col < len(row):
                    rows.append(row)
        else:
            # No header: treat the first row as data and sniff the URL column.
            data = [header] + [r for r in reader]
            guess = None
            for row in data[:5]:
                for i, cell in enumerate(row):
                    if normalize_url(cell):
                        guess = i
                        break
                if guess is not None:
                    break
            if guess is None:
                return 0
            url_col = guess
            rows = data

        count = 0
        for row in rows:
            if url_col >= len(row):
                continue
            title = row[title_col] if title_col is not None and title_col < len(row) else None
            folder = row[folder_col] if folder_col is not None and folder_col < len(row) else None
            added = None
            if date_col is not None and date_col < len(row):
                added = date_unix(row[date_col])
            sink.add(row[url_col], title, folder, added)
            count += 1
        return count


_TEXT_URL = re.compile(
    r"(?:https?|ftps?)://[^\s\"'<>)\]},]+|mailto:[^\s\"'<>)\]},]+|magnet:\?[^\s\"'<>]+",
    re.IGNORECASE,
)
_MD_LINK = re.compile(r"\[([^\]]{0,300})\]\(\s*(?:https?://[^)\s]+)\s*\)")


def _emit_text_block(block: str, sink: Sink) -> int:
    count = 0
    for line in block.splitlines():
        if not line.strip():
            continue
        md_titles = {m.group(1).strip().lower() for m in _MD_LINK.finditer(line)}
        for match in _TEXT_URL.finditer(line):
            url = match.group(0).rstrip(".,;:")
            title = next((t for t in md_titles if t), None)
            sink.add(url, title, None, None)
            count += 1
    return count


def _parse_text(path: str, sink: Sink):
    """Plain text, Markdown or an exported list. One or many links per line.

    Reads fixed-size chunks rather than iterating lines: a minified 300 MB dump
    can arrive as one enormous line, and line iteration would pull it all in.
    Splitting only at newlines keeps URLs intact across chunk boundaries.
    """
    count = 0
    carry = ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            data = carry + chunk
            cut = data.rfind("\n")
            if cut == -1:
                carry = data
                if len(carry) > (1 << 23):  # pathological single line: digest it
                    count += _emit_text_block(carry, sink)
                    carry = ""
                continue
            count += _emit_text_block(data[:cut], sink)
            carry = data[cut:]
        if carry:
            count += _emit_text_block(carry, sink)
    return count


def _parse_url_shortcut(path: str, sink: Sink):
    """Windows .url file: an INI with a URL= key.

    These are written by hand (UTF-8 or ANSI) and by newer tools (UTF-16). A
    UTF-16 file decoded as UTF-8 keeps a NUL between every character, so the
    URL= line never matches and the shortcut silently yields nothing. The bytes
    decide the encoding here, not an assumption.
    """
    with open(path, "rb") as fh:
        raw = fh.read(65536)  # URL= sits in the first few bytes of any real file
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw[2:].decode("utf-16-le" if raw[:2] == b"\xff\xfe" else "utf-16-be", errors="replace")
    elif b"\x00" in raw[:128]:
        text = raw.decode("utf-16-le", errors="replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")  # IE writes ANSI
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("url="):
            sink.add(line[4:], os.path.splitext(os.path.basename(path))[0], None, None)
            return 1
    return 0


# Streaming JSON: tokenise the text instead of materialising it. json.load on a
# 400 MB Chrome profile builds a multi-gigabyte Python object graph; this keeps
# only the chain of open objects (folder names) plus one token in memory.
_JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]:,]')
_JSON_SCALAR = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")
_URL_KEY_SET = {k.lower() for k in URL_KEYS}
_NAME_KEY_SET = {k.lower() for k in TITLE_KEYS}
_DATE_KEY_SET = {"date_added", "dateadded", "added", "created", "last_modified"}


def _scan_json_stream(fh, sink: Sink, chunk: int = 1 << 20) -> int:
    """Emit every URL-bearing object in a JSON file, in bounded memory.

    A stack of open objects carries the folder names and each object's own
    name/url/date; a bookmark is emitted when its closing brace arrives. Nested
    folders therefore still work, and memory does not grow with file size.
    """
    buf = ""
    pos = 0
    eof = False
    frames = []
    pending = None
    last_string = None

    def more():
        nonlocal buf, eof
        data = fh.read(chunk)
        if not data:
            eof = True
            return False
        buf += data
        return True

    def emit(frame):
        ancestors = [f.get("name") for f in frames if not f.get("array")]
        folder = "/".join(n for n in ancestors if n and n.lower() not in CHROME_ROOTS)
        added = date_chrome(frame.get("added")) if frame.get("added") else None
        sink.add(frame["url"], frame.get("name"), folder, added)

    while True:
        if pos > (1 << 22):  # compact without ever splitting a token
            buf = buf[pos:]
            pos = 0
        while pos < len(buf) and buf[pos] in " \t\r\n":
            pos += 1
        if pos >= len(buf):
            if not more():
                break
            continue

        tok_match = _JSON_TOKEN.match(buf, pos)
        if tok_match is None:
            scalar = _JSON_SCALAR.match(buf, pos)
            if scalar is None:
                # An opening quote means the string was cut by the chunk boundary;
                # skipping it as junk would lose the whole token. Read on instead.
                if not eof and (buf[pos] == '"' or len(buf) - pos < 64):
                    if more():
                        continue
                pos += 1  # genuinely stray punctuation in a malformed file
                continue
            if scalar.end() == len(buf) and not eof:
                more()
                continue
            if pending is not None and frames and not frames[-1].get("array"):
                if pending in _DATE_KEY_SET:
                    frames[-1]["added"] = scalar.group(0)
            pending = None
            pos = scalar.end()
            continue

        if tok_match.end() == len(buf) and not eof:
            more()  # the token may be cut by the chunk boundary; retry once extended
            continue

        tok = tok_match.group(0)
        pos = tok_match.end()

        if tok == ":":
            pending = last_string.lower() if isinstance(last_string, str) else None
            last_string = None
        elif tok == ",":
            last_string = None
        elif tok in "{[":
            frames.append({"array": tok == "["})
            pending = None
            last_string = None
        elif tok in "}]":
            if frames:
                frame = frames.pop()
                if frame.get("url"):
                    emit(frame)
            pending = None
            last_string = None
        else:
            try:
                value = json.loads(tok)
            except json.JSONDecodeError:
                continue
            if pending is not None:
                if frames and not frames[-1].get("array"):
                    if pending in _URL_KEY_SET:
                        frames[-1]["url"] = value
                    elif pending in _NAME_KEY_SET:
                        frames[-1]["name"] = value
                    elif pending in _DATE_KEY_SET:
                        frames[-1]["added"] = value
                pending = None
                last_string = None
            else:
                last_string = value
    return sink.rows_seen


def _looks_like_html(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096).lower()
    except OSError:
        return False
    return "<a href" in head or "netscape-bookmark" in head or "<dl" in head


def _parse_json_file(path: str, sink: Sink):
    before = getattr(sink, "rows_seen", 0)
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        _scan_json_stream(fh, sink)
    return getattr(sink, "rows_seen", 0) - before


def _parse_html_file(path: str, sink: Sink):
    parser = NetscapeParser(sink)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            parser.feed(chunk)
    parser.close()
    return sink.rows_seen


HTML_EXT = {".html", ".htm", ".shtml", ".xhtml"}
JSON_EXT = {".json", ".jsonl", ".bak", ".backup"}
CSV_EXT = {".csv", ".tsv"}
TEXT_EXT = {".txt", ".md", ".markdown", ".lst", ".list", ".urls"}
URL_EXT = {".url"}
SQLITE_EXT = {".sqlite", ".sqlite3", ".db", ".places"}
BROWSER_NAMES = {"bookmarks", "bookmarks.bak", "places.sqlite", "bookmarks.json", "bookmarks.html"}


# A whole drive is full of .html, .csv and .txt files that have nothing to do
# with bookmarks, so generic extensions need a name or folder hint. Scanning a
# 3 TB disk should not open every text file on it.
NAME_HINTS = ("bookmark", "favorit", "links", "urls")
GENERIC_EXT = HTML_EXT | JSON_EXT | CSV_EXT | TEXT_EXT


# Names that mark a SQLite file as a browser profile. Every application on a
# disk ships .db files, and so does bmvault itself, so extension alone is not
# enough to justify opening one.
SQLITE_HINTS = ("places", "bookmark", "favorit", "firefox")


def looks_like_candidate(name: str, relaxed: bool = False) -> bool:
    low = name.lower()
    if low in BROWSER_NAMES:
        return True
    ext = os.path.splitext(low)[1]
    if ext in URL_EXT:
        return True  # nothing else writes Internet Shortcuts
    if ext in SQLITE_EXT:
        return any(hint in low for hint in SQLITE_HINTS) or relaxed
    if any(hint in low for hint in NAME_HINTS):
        return True
    return relaxed and ext in GENERIC_EXT


def parse_file(path: str, sink: Sink):
    """Dispatch on extension, with fallbacks. Returns (format_name, note)."""
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path).lower()

    if ext in SQLITE_EXT or name == "places.sqlite":
        return "firefox-sqlite", _parse_firefox(path, sink)
    if ext == ".url":
        return "url-shortcut", _parse_url_shortcut(path, sink)
    if ext in CSV_EXT:
        return "csv", _parse_csv(path, sink)
    if ext == ".jsonl":
        n = 0
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    _walk_json(json.loads(line), "", lambda u, t, f, a: sink.add(u, t, f, a))
                    n += 1
                except json.JSONDecodeError:
                    continue
        return "jsonl", n
    # HTML first: "bookmarks.html" and "Bookmarks.bak" both match the browser
    # filename patterns, and the JSON scanner is tolerant enough to accept an
    # HTML file and silently find nothing in it.
    if ext in HTML_EXT:
        return "netscape-html", _parse_html_file(path, sink)
    # Chrome's profile file is literally named "Bookmarks" with no extension;
    # any other extension is a stronger signal than the filename.
    if ext in JSON_EXT or (ext == "" and name.startswith("bookmarks")):
        before = getattr(sink, "rows_seen", 0)
        fmt, count = "browser-json", _parse_json_file(path, sink)
        if getattr(sink, "rows_seen", 0) == before and _looks_like_html(path):
            return "netscape-html", _parse_html_file(path, sink)
        return fmt, count
    if ext in TEXT_EXT:
        return "text", _parse_text(path, sink)

    # Last resort: sniff the first kilobyte.
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(4096)
    if "<a href" in head.lower() or "netscape-bookmark" in head.lower():
        return "netscape-html", _parse_html_file(path, sink)
    if head.lstrip()[:1] in "{[":
        try:
            return "browser-json", _parse_json_file(path, sink)
        except json.JSONDecodeError:
            return "unknown", 0
    return "text", _parse_text(path, sink)


# --------------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY,
    path         TEXT UNIQUE NOT NULL,
    format       TEXT,
    bytes        INTEGER,
    mtime        REAL,
    scanned_at   TEXT,
    n_bookmarks  INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS bookmarks (
    id       INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    url      TEXT NOT NULL,
    norm_url TEXT NOT NULL,
    title    TEXT,
    folder   TEXT,
    added    TEXT
);
CREATE INDEX IF NOT EXISTS ix_bookmarks_norm   ON bookmarks(norm_url);
CREATE INDEX IF NOT EXISTS ix_bookmarks_source ON bookmarks(source_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS bookmarks_fts USING fts5(
    title, url, folder, content='bookmarks', content_rowid='id'
);
"""


def connect(path: str, create: bool = True):
    if not create and not os.path.exists(path):
        sys.exit(f"no database at {path} -- run 'scan' first")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA)
        conn.commit()
    except sqlite3.OperationalError:
        pass  # SQLite built without FTS5; search falls back to LIKE.
    return conn


def fts_ready(conn):
    try:
        conn.execute("SELECT count(*) FROM bookmarks_fts").fetchone()
        return True
    except sqlite3.Error:
        return False


class Store(Sink):
    """Buffered writer into the bookmarks table for one source file."""

    def __init__(self, conn, source_id):
        self.conn = conn
        self.source_id = source_id
        self.buf = []
        self.rows_seen = 0
        self.stored = 0
        self.skipped = 0

    def add(self, url, title=None, folder=None, added=None):
        self.rows_seen += 1
        r = normalize_url(url)
        if not r:
            self.skipped += 1
            return
        key, display = r
        self.buf.append((self.source_id, display, key, _text(title), _text(folder, 400), added))
        if len(self.buf) >= BATCH:
            self.flush()

    def flush(self):
        if not self.buf:
            return
        self.conn.executemany(
            "INSERT INTO bookmarks(source_id, url, norm_url, title, folder, added) VALUES (?,?,?,?,?,?)",
            self.buf,
        )
        self.stored += len(self.buf)
        self.buf.clear()


# ------------------------------------------------------------------ scanning

# Directories that never hold bookmarks and can contain millions of files.
# Hidden folders are deliberately NOT pruned wholesale: Firefox lives in
# ~/.mozilla and Chrome in ~/.config/google-chrome, so blanket dot-skipping is
# exactly how a whole-disc scan misses the two most important profiles.
SKIP_DIRS = {
    # version control, build and package noise
    ".git", ".svn", ".hg", ".cache", ".npm", ".cargo", ".rustup",
    ".venv", ".gradle", ".m2", "node_modules", "__pycache__",
    # trash / index metadata
    ".trash", ".trashes", "$recycle.bin", "system volume information",
    "lost+found", ".spotlight-v100", ".fseventsd", ".documentrevisions-v100",
    # operating system trees
    "windows", "windowsapps", "winsxs", "program files",
    "program files (x86)", "programdata", "recovery", "perflogs",
    "$windows.~bt", "$windows.~ws",
    # Legacy junctions Windows keeps for compatibility with old programs.
    # "Application Data" points back at the folder that contains it, so walking
    # it recurses until MAX_PATH; the others duplicate huge trees.
    "application data", "local settings", "my documents",
    "documents and settings", "default user", "all users",
}

# /proc, /sys, /dev and /run are endless or meaningless to walk -- but a folder
# that merely happens to be called "dev" (D:\dev, ~/dev) is somebody's source
# tree, so these are pruned only at the filesystem root, and only on POSIX.
ROOT_ONLY_SKIP = {"proc", "sys", "dev", "run"} if os.name != "nt" else set()


def _folder_is_relaxed(dirpath: str) -> bool:
    parts = dirpath.replace("\\", "/").lower().split("/")
    return any("bookmark" in p or "favorit" in p for p in parts)


def _expand_root(root) -> str:
    """Tidy a user-supplied root before walking it.

    On Windows "D:" is not the drive -- it is the current directory *on* D:, so
    a whole-drive scan needs the separator. Env vars and ~ are expanded because
    they are how people point at a profile: %APPDATA%\\Mozilla, ~/Desktop.
    """
    r = os.path.expandvars(os.path.expanduser(str(root)))
    if re.fullmatch(r"[A-Za-z]:", r):
        r += os.sep
    return r


def iter_files(roots, max_bytes, follow=False, any_name=False, exclude=()):
    excluded = {os.path.abspath(p) for p in exclude if p}
    for root in map(_expand_root, roots):
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow, onerror=lambda e: None):
            skip = SKIP_DIRS
            if ROOT_ONLY_SKIP and os.path.abspath(dirpath) == os.sep:
                skip = SKIP_DIRS | ROOT_ONLY_SKIP
            dirnames[:] = [d for d in dirnames if d.lower() not in skip]
            relaxed = any_name or _folder_is_relaxed(dirpath)
            for name in filenames:
                if not looks_like_candidate(name, relaxed):
                    continue
                path = os.path.join(dirpath, name)
                if os.path.abspath(path) in excluded:
                    continue
                try:
                    if os.path.getsize(path) > max_bytes:
                        continue
                    if follow is False and os.path.islink(path):
                        continue
                except OSError:
                    continue
                yield path


def cmd_scan(args):
    conn = connect(args.db)
    max_bytes = int(args.max_size * 1024 * 1024)
    # Never ingest the database being written, even if it sits on the scanned drive.
    files = list(iter_files(args.roots, max_bytes, args.follow, args.any_name,
                            exclude=(args.db, args.db + "-wal", args.db + "-shm")))
    if args.limit:
        files = files[: args.limit]
    log(f"{len(files)} candidate files under {', '.join(args.roots)}", args)

    totals = Counter()
    for i, path in enumerate(files, 1):
        try:
            stat = os.stat(path)
        except OSError as exc:
            log(f"  skip {path}: {exc}", args)
            totals["unreadable"] += 1
            continue

        conn.execute(
            "INSERT INTO sources(path, format, bytes, mtime, scanned_at, n_bookmarks, note) "
            "VALUES (?,?,?,?,?,0,NULL) "
            "ON CONFLICT(path) DO UPDATE SET bytes=excluded.bytes, mtime=excluded.mtime, "
            "scanned_at=excluded.scanned_at, note=NULL",
            (path, None, stat.st_size, stat.st_mtime, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        row = conn.execute("SELECT id FROM sources WHERE path = ?", (path,)).fetchone()
        source_id = row[0]
        conn.execute("DELETE FROM bookmarks WHERE source_id = ?", (source_id,))

        store = Store(conn, source_id)
        try:
            fmt, _ = parse_file(path, store)
            store.flush()
            conn.execute(
                "UPDATE sources SET format = ?, n_bookmarks = ? WHERE id = ?",
                (fmt, store.stored, source_id),
            )
            conn.commit()
            totals[fmt] += 1
            totals["rows"] += store.rows_seen
            totals["stored"] += store.stored
            if args.progress and (i % 25 == 0 or i == len(files)):
                log(f"  [{i}/{len(files)}] {store.stored:>7} rows  {path}", args)
        except Exception as exc:  # a corrupt file must not stop a 200-file sweep
            store.flush()
            conn.execute("UPDATE sources SET note = ? WHERE id = ?", (f"{type(exc).__name__}: {exc}"[:300], source_id))
            conn.commit()
            totals["errors"] += 1
            log(f"  error {path}: {type(exc).__name__}: {exc}", args)

    if fts_ready(conn):
        conn.execute("INSERT INTO bookmarks_fts(bookmarks_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()

    log(
        f"done: {totals['stored']} bookmarks from {len(files)} files "
        f"({totals['errors']} errors) -> {args.db}",
        args,
    )


# ----------------------------------------------------------------- reporting

def _human(n):
    return f"{n:,}"


def cmd_report(args):
    conn = connect(args.db, create=False)
    total = conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
    unique = conn.execute("SELECT count(DISTINCT norm_url) FROM bookmarks").fetchone()[0]
    files = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
    errors = conn.execute("SELECT count(*) FROM sources WHERE note IS NOT NULL").fetchone()[0]
    dup_rows = total - unique
    pct = (dup_rows / total * 100) if total else 0.0

    print(f"database : {os.path.abspath(args.db)}")
    print(f"sources  : {_human(files)} files ({_human(errors)} with errors)")
    print(f"rows     : {_human(total)} bookmark records")
    print(f"unique   : {_human(unique)} distinct URLs")
    print(f"dupes    : {_human(dup_rows)} redundant copies ({pct:.1f}% of rows)")

    formats = conn.execute(
        "SELECT coalesce(format,'?'), count(*), coalesce(sum(n_bookmarks),0) FROM sources GROUP BY 1 ORDER BY 3 DESC"
    ).fetchall()
    if formats:
        print("\nby format:")
        for fmt, n, rows in formats:
            print(f"  {fmt:<16} {_human(n):>6} files  {_human(rows):>9} rows")

    if args.sources:
        print("\nfiles:")
        for path, rows, note in conn.execute(
            "SELECT path, n_bookmarks, note FROM sources ORDER BY n_bookmarks DESC"
        ):
            flag = f"  [{note}]" if note else ""
            print(f"  {_human(rows):>9}  {path}{flag}")

    if args.duplicates:
        print(f"\ntop {args.duplicates} duplicated URLs:")
        rows = conn.execute(
            "SELECT norm_url, count(*) c, count(DISTINCT source_id) s, min(title) "
            "FROM bookmarks GROUP BY norm_url HAVING c > 1 ORDER BY c DESC, s DESC LIMIT ?",
            (args.duplicates,),
        ).fetchall()
        for url, n, srcs, title in rows:
            label = f"  {title[:60]}" if title else ""
            print(f"  {n:>3}x in {srcs:>3} files  {url}{label}")
        if not rows:
            print("  none")

    if args.folders:
        print(f"\ntop {args.folders} folders:")
        for folder, n in conn.execute(
            "SELECT folder, count(*) c FROM bookmarks WHERE folder IS NOT NULL AND folder != '' "
            "GROUP BY folder ORDER BY c DESC LIMIT ?",
            (args.folders,),
        ):
            print(f"  {_human(n):>8}  {folder[:70]}")

    years = conn.execute(
        "SELECT substr(added,1,4) y, count(*) FROM bookmarks WHERE added IS NOT NULL "
        "GROUP BY y ORDER BY y DESC LIMIT ?",
        (args.years,),
    ).fetchall()
    if years:
        print("\nby added year:")
        for y, n in years:
            print(f"  {y}  {_human(n):>8}")
    conn.close()


def cmd_search(args):
    conn = connect(args.db, create=False)
    query = args.query.strip()
    if fts_ready(conn):
        terms = re.findall(r"[A-Za-z0-9_]+", query)
        if not terms:
            sys.exit("search needs at least one word or number")
        # Prefix-match each term so "rust" also finds "rustacean".
        expr = " ".join(f'"{t}"*' for t in terms)
        sql = (
            "SELECT b.url, b.title, b.folder, b.added, b.norm_url FROM bookmarks_fts f "
            "JOIN bookmarks b ON b.id = f.rowid WHERE bookmarks_fts MATCH ? "
            "ORDER BY rank LIMIT ?"
        )
        params = (expr, args.limit * 8)
    else:
        like = f"%{query}%"
        sql = (
            "SELECT url, title, folder, added, norm_url FROM bookmarks "
            "WHERE url LIKE ? OR title LIKE ? OR folder LIKE ? ORDER BY url LIMIT ?"
        )
        params = (like, like, like, args.limit * 8)

    seen, shown = set(), 0
    for url, title, folder, added, norm in conn.execute(sql, params):
        if norm in seen:
            continue
        seen.add(norm)
        meta = " | ".join(x for x in (folder, added) if x)
        print(f"{url}\n    {title or '(no title)'}{'  [' + meta + ']' if meta else ''}")
        shown += 1
        if shown >= args.limit:
            break
    if not shown:
        print("no matches")
    conn.close()


# ------------------------------------------------------------------- export

# Sorting on "A/child" as "A\x01child" keeps a folder's whole subtree together
# ("A" < "A/child" < "A-other"), which is what lets the exporter emit folder
# blocks in one pass instead of building a tree.
_FOLDER_ORDER = "replace(coalesce(folder,''), '/', char(1)) COLLATE NOCASE, coalesce(title,'') COLLATE NOCASE"

# A global sort cannot keep folder blocks contiguous, so everything except
# "folder" produces one flat, alphabetically ordered list.
_SORT_ORDERS = {
    "folder": _FOLDER_ORDER,
    "title": "coalesce(nullif(trim(coalesce(title,'')),''), url) COLLATE NOCASE, norm_url",
    # Order by the URL as written, not the dedupe key: the file is read by a human,
    # and a stripped "www." would sort a link somewhere unexpected.
    "url": "url COLLATE NOCASE, norm_url",
    "added": "coalesce(nullif(added,''), '9999-99-99') ASC, coalesce(title,'') COLLATE NOCASE",
}

NETSCAPE_HEADER = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<!-- This is an automatically generated file. It will be read and overwritten.
     DO NOT EDIT! -->
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
"""


def _rows_for_export(conn, dedupe, sort="folder"):
    """Rows to export. Dedupe keeps one row per distinct URL, preferring the
    longest title (exports usually truncate titles, so longest is the richest)."""
    order = _SORT_ORDERS.get(sort, _FOLDER_ORDER)
    if dedupe:
        sql = (
            "SELECT norm_url, url, title, folder, added, copies FROM ("
            "  SELECT b.*, "
            "    row_number() OVER (PARTITION BY norm_url"
            "                       ORDER BY length(coalesce(title,'')) DESC, id) rn, "
            "    count(*) OVER (PARTITION BY norm_url) copies "
            "  FROM bookmarks b"
            ") WHERE rn = 1 ORDER BY " + order
        )
    else:
        sql = "SELECT norm_url, url, title, folder, added, 1 FROM bookmarks ORDER BY " + order
    return conn.execute(sql)


def _add_stamp(added):
    if not added:
        return ""
    try:
        epoch = int(datetime.fromisoformat(added).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return ""
    return f' ADD_DATE="{epoch}"'


class NetscapeWriter:
    """Writes bookmark rows as Netscape HTML, rotating into new files at a limit.

    Rotation happens between entries, never inside one, and every part is a
    complete, importable file: its own header, its own folder blocks, closed
    properly. That is why a 500k-entry export can be cut into files a browser
    will actually swallow.
    """

    def __init__(self, path_for_part, max_entries=0, max_bytes=0):
        self.path_for_part = path_for_part
        self.max_entries = max_entries or 0
        self.max_bytes = max_bytes or 0
        self.out = None
        self.stack = []
        self.entries = 0
        self.size = 0
        self.parts = []

    def open(self):
        path = self.path_for_part(len(self.parts) + 1)
        self.out = open(path, "w", encoding="utf-8")
        self.parts.append(path)
        self.write(NETSCAPE_HEADER)
        self.stack = []
        self.entries = 0
        self.size = 0

    def write(self, text):
        self.out.write(text)
        self.size += len(text)

    def rotate_due(self):
        if self.max_entries and self.entries >= self.max_entries:
            return True
        return bool(self.max_bytes and self.size >= self.max_bytes and self.entries)

    def sync_folders(self, target):
        common = 0
        while common < len(self.stack) and common < len(target) and self.stack[common] == target[common]:
            common += 1
        while len(self.stack) > common:
            self.stack.pop()
            self.write("    " * (len(self.stack) + 1) + "</DL><p>\n")
        for name in target[common:]:
            pad = "    " * (len(self.stack) + 1)
            self.write(f"{pad}<DT><H3>{html.escape(name)}</H3>\n{pad}<DL><p>\n")
            self.stack.append(name)

    def add(self, url, title, added, folder, grouped=True):
        if self.out is None or self.rotate_due():
            if self.out is not None:
                self.finish()
            self.open()
        target = [part.strip() for part in (folder or "").split("/") if part.strip()] if grouped else []
        self.sync_folders(target)
        pad = "    " * (len(self.stack) + 1)
        self.write(
            f'{pad}<DT><A HREF="{html.escape(url, quote=True)}"{_add_stamp(added)}>'
            f"{html.escape(title or url)}</A>\n"
        )
        self.entries += 1

    def finish(self):
        if self.out is None:
            return
        while self.stack:
            self.stack.pop()
            self.write("    " * (len(self.stack) + 1) + "</DL><p>\n")
        self.out.write("</DL><p>\n")
        self.out.close()
        self.out = None


def _rotate_due(count, size, max_entries, max_bytes):
    if max_entries and count >= max_entries:
        return True
    return bool(max_bytes and size >= max_bytes and count)


def cmd_split(args):
    """Split one merged output, or the database itself, into files of a set size."""
    db = args.db
    scratch = None
    if args.source and not args.source.lower().endswith((".db", ".sqlite", ".sqlite3")):
        scratch = tempfile.mkdtemp(prefix="bmvault-split-")
        db = os.path.join(scratch, "split.db")
        cmd_scan(argparse.Namespace(
            roots=[args.source], db=db, max_size=1024.0, follow=False,
            limit=0, any_name=True, progress=False, func=cmd_scan,
        ))

    max_entries = args.max_entries or 0
    max_bytes = int(args.max_size * 1024 * 1024) if args.max_size else 0
    if not max_entries and not max_bytes:
        max_entries = 50000  # a sane default beats one unusable giant file
    os.makedirs(args.out_dir, exist_ok=True)
    stem = args.name or "bookmarks"
    parts = []
    written = 0

    conn = None
    try:
        conn = connect(db, create=False)
        cursor = _rows_for_export(conn, dedupe=not args.all, sort=args.sort)
        grouped = args.sort == "folder"

        if args.format == "html":
            writer = NetscapeWriter(
                lambda index: os.path.join(args.out_dir, f"{stem}-{index:03d}.html"),
                max_entries=max_entries, max_bytes=max_bytes,
            )
            for row in cursor:
                writer.add(row[1], row[2], row[4], row[3], grouped=grouped)
                written += 1
            writer.finish()
            parts = writer.parts
        else:
            ext = "csv" if args.format == "csv" else "jsonl"
            fh = None
            count = size = 0
            csv_out = None
            for row in cursor:
                url, title, folder, added, copies = row[1], row[2], row[3], row[4], row[5]
                if fh is None or _rotate_due(count, size, max_entries, max_bytes):
                    if fh is not None:
                        fh.close()
                    path = os.path.join(args.out_dir, f"{stem}-{len(parts) + 1:03d}.{ext}")
                    fh = open(path, "w", encoding="utf-8", newline="")
                    parts.append(path)
                    count = size = 0
                    if args.format == "csv":
                        csv_out = csv.writer(fh)
                        csv_out.writerow(["url", "title", "folder", "added", "copies"])
                if args.format == "csv":
                    fields = [url, title or "", folder or "", added or "", copies]
                    csv_out.writerow(fields)
                    size += sum(len(str(f)) for f in fields) + 8
                else:
                    line = json.dumps({"url": url, "title": title, "folder": folder,
                                       "added": added, "copies": copies}, ensure_ascii=False)
                    fh.write(line + "\n")
                    size += len(line) + 1
                count += 1
                written += 1
            if fh is not None:
                fh.close()
    finally:
        if conn is not None:
            conn.close()
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    print(f"wrote {_human(written)} {'unique' if not args.all else 'total'} bookmarks "
          f"as {len(parts)} files in {args.out_dir}")
    for path in parts:
        print(f"  {os.path.basename(path):<28} {os.path.getsize(path) / 1024:7.1f} KB")


def cmd_merge(args):
    """Scan everything, then write one merged file -- the whole errand in a step."""
    cmd_scan(argparse.Namespace(
        roots=args.roots, db=args.db, max_size=args.max_size, follow=args.follow,
        limit=0, any_name=args.any_name, progress=args.progress, func=cmd_scan,
    ))
    cmd_export(argparse.Namespace(
        db=args.db, out=args.out, format=args.format, all=args.all, sort=args.sort, func=cmd_export,
    ))


def cmd_export(args):
    conn = connect(args.db, create=False)
    dedupe = not args.all
    sort = getattr(args, "sort", "folder")
    cursor = _rows_for_export(conn, dedupe, sort)  # iterated, never fetched all at once
    written = 0

    if args.format == "html":
        writer = NetscapeWriter(lambda _index: args.out)
        for row in cursor:
            writer.add(row[1], row[2], row[4], row[3], grouped=(sort == "folder"))
        writer.finish()
        written = writer.entries
    elif args.format == "csv":
        with open(args.out, "w", encoding="utf-8", newline="") as out:
            w = csv.writer(out)
            w.writerow(["url", "title", "folder", "added", "copies"])
            for _k, url, title, folder, added, copies in cursor:
                w.writerow([url, title or "", folder or "", added or "", copies])
                written += 1
    elif args.format == "jsonl":
        with open(args.out, "w", encoding="utf-8") as out:
            for _k, url, title, folder, added, copies in cursor:
                out.write(json.dumps(
                    {"url": url, "title": title, "folder": folder, "added": added, "copies": copies},
                    ensure_ascii=False,
                ) + "\n")
                written += 1
    else:
        sys.exit(f"unknown format {args.format}")

    print(f"wrote {_human(written)} {'unique' if dedupe else 'total'} bookmarks to {args.out}")
    conn.close()


# ---------------------------------------------------------------------- CLI

def log(message, args=None):
    if args is None or getattr(args, "progress", True):
        print(message, file=sys.stderr, flush=True)


def build_parser():
    p = argparse.ArgumentParser(
        prog="bmvault",
        description="Consolidate, de-duplicate and search bookmark files from many drives.",
    )
    p.add_argument("--version", action="version", version=f"bmvault {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="walk folders and ingest every bookmark file found")
    s.add_argument("roots", nargs="+", help="folders (or single files) to scan")
    s.add_argument("--db", default="vault.db", help="database path (default: vault.db)")
    s.add_argument("--max-size", type=float, default=1024.0,
                   help="skip files larger than this many MB (default 1024; parsing is streaming)")
    s.add_argument("--follow", action="store_true", help="follow symlinks and directory junctions")
    s.add_argument("--limit", type=int, default=0, help="stop after N candidate files (dry runs)")
    s.add_argument(
        "--any-name", action="store_true",
        help="also treat any .html/.json/.csv/.txt as a candidate, not just bookmark-named files",
    )
    s.add_argument("--quiet", dest="progress", action="store_false", help="only print the final summary")
    s.set_defaults(func=cmd_scan, progress=True)

    r = sub.add_parser("report", help="summarise the database and show duplicates")
    r.add_argument("--db", default="vault.db")
    r.add_argument("--duplicates", type=int, default=10, help="how many duplicate URLs to list (0 to skip)")
    r.add_argument("--folders", type=int, default=8, help="how many folders to list (0 to skip)")
    r.add_argument("--years", type=int, default=0, help="show up to N years of add dates")
    r.add_argument("--sources", action="store_true", help="list every scanned file")
    r.set_defaults(func=cmd_report)

    q = sub.add_parser("search", help="full-text search titles, URLs and folders")
    q.add_argument("query")
    q.add_argument("--db", default="vault.db")
    q.add_argument("--limit", type=int, default=25)
    q.set_defaults(func=cmd_search)

    m = sub.add_parser("merge", help="scan drives and write one merged bookmark file in a single step")
    m.add_argument("roots", nargs="+", help="folders, drives or single files to scan")
    m.add_argument("-o", "--out", default="all-bookmarks.html", help="output file (default: all-bookmarks.html)")
    m.add_argument("--format", choices=("html", "csv", "jsonl"), default="html")
    m.add_argument("--db", default="vault.db", help="database kept for later report/search")
    m.add_argument("--all", action="store_true", help="keep duplicate rows instead of one per URL")
    m.add_argument("--sort", choices=("folder", "title", "url", "added"), default="folder",
                   help="entry order: folder keeps folder blocks (default); others give a flat sorted list")
    m.add_argument("--max-size", type=float, default=1024.0, help="skip files larger than this many MB")
    m.add_argument("--follow", action="store_true", help="follow symlinks and junctions")
    m.add_argument("--any-name", action="store_true",
                   help="also read .html/.json/.csv/.txt files without a bookmark-ish name")
    m.add_argument("--quiet", dest="progress", action="store_false", help="only print the final summary")
    m.set_defaults(func=cmd_merge, progress=True)

    sp = sub.add_parser("split", help="split one merged file into files of a given size")
    sp.add_argument("source", nargs="?", help="merged .html/.csv/.jsonl to split (default: use --db)")
    sp.add_argument("--db", default="vault.db")
    sp.add_argument("--out-dir", default="parts", help="directory for the parts (default: parts)")
    sp.add_argument("--name", default="bookmarks", help="filename stem (default: bookmarks)")
    sp.add_argument("--max-entries", type=int, default=0, help="bookmarks per file")
    sp.add_argument("--max-size", type=float, default=0, help="megabytes per file")
    sp.add_argument("--format", choices=("html", "csv", "jsonl"), default="html")
    sp.add_argument("--sort", choices=("folder", "title", "url", "added"), default="folder")
    sp.add_argument("--all", action="store_true", help="keep duplicate rows")
    sp.set_defaults(func=cmd_split)

    e = sub.add_parser("export", help="write a merged, de-duplicated bookmark file")
    e.add_argument("--format", choices=("html", "csv", "jsonl"), default="html")
    e.add_argument("-o", "--out", required=True)
    e.add_argument("--db", default="vault.db")
    e.add_argument("--all", action="store_true", help="keep every row instead of one per URL")
    e.add_argument("--sort", choices=("folder", "title", "url", "added"), default="folder",
                   help="entry order: folder keeps folder blocks (default); others give a flat sorted list")
    e.set_defaults(func=cmd_export)
    return p


def _utf8_console():
    """Windows consoles are cp1252 and raise on a path full of CJK or emoji.

    A whole-drive scan must not die because somebody has a folder called
    "图书" or a bookmark file with an emoji in the name; the replacement
    character in a log line is a fair price.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # already a binary stream, or replaced by a test harness


def main(argv=None):
    _utf8_console()
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
