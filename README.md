# bmvault

Consolidate, de-duplicate and search bookmark files that live on many drives.

One Python file, standard library only, no install step. Copy `bmvault.py` to a
USB stick or any machine with Python 3.8+ and run it there.

## Why

Bookmarks accumulate in per-browser exports, old profile folders, backups,
`.url` shortcuts and hand-kept text lists. The same page ends up saved a dozen
times, each copy with a different title, folder and tracking parameter. bmvault
reads all of it into one SQLite database, keyed on a normalised URL, so you can
answer "what do I actually have?" before deciding what to keep.

## Use

One command, whole disc, one output file:

```bash
python3 bmvault.py merge /mnt/old-hdd -o all-bookmarks.html
# Windows:  python bmvault.py merge D:\ E:\ -o all-bookmarks.html
```

That scans every candidate file under the roots, writes `all-bookmarks.html`
(importable by any browser) and keeps `vault.db` so you can report, search and
export again without re-reading the drives.

Or step by step:

```bash
# 1. point it at the drives, mounts or folders
python3 bmvault.py scan /mnt/backup /media/old-hdd "D:/Users/me" --db vault.db

# 2. see what came in and what is redundant
python3 bmvault.py report --db vault.db --duplicates 20 --sources

# 3. find things
python3 bmvault.py search "rust async" --db vault.db

# 4. write one clean file you can re-import into any browser
python3 bmvault.py export --format html -o merged.html --db vault.db

# 5. cut that file into parts a browser will swallow
python3 bmvault.py split merged.html --out-dir parts --max-entries 20000
```

`scan` is idempotent: re-running it replaces each file's rows rather than
duplicating them, so it is safe to point at the same drives repeatedly as you
find more files.

### Options worth knowing

| flag | why |
| --- | --- |
| `--any-name` | by default only bookmark-ish filenames are opened (see below); this opens every `.html`, `.json`, `.csv`, `.txt` too |
| `--limit N` | dry run over the first N candidate files |
| `--max-size MB` | skip huge files (default 1024 MB; parsing is streaming, so this is only a guard) |
| `--quiet` | suppress per-file progress on stderr |
| `export --all` | keep every row instead of one per distinct URL |
| `export --sort folder\|title\|url\|added` | entry order; `folder` (default) keeps folder blocks, the others give one flat alphabetical list |
| `split --max-entries N` / `--max-size MB` | cut the merged file into independently importable parts |

## Windows

`bmvault.py` and `gui.py` are plain Python 3.8+: no compiler, no package, no
installer. Use `py -3` (or `python`) instead of `python3`:

```bat
py -3 bmvault.py merge D:\ E:\ -o all-bookmarks.html
py -3 gui.py --db vault.db --port 8000
```

What is handled for you on a Windows disc:

* **Drive roots.** `D:` in a Windows command line means *the current directory on
  D:*, not the drive, so it is expanded to `D:\`. Either spelling works.
* **Profiles inside AppData.** Hidden folders are searched, so a plain scan of
  `C:\Users` finds
  `%LOCALAPPDATA%\Google\Chrome\User Data\Default\Bookmarks`,
  `%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Bookmarks` and
  `%APPDATA%\Mozilla\Firefox\Profiles\*\places.sqlite`. `%VARS%` and `~` are
  expanded.
* **Legacy junctions.** `Application Data`, `Local Settings`, `My Documents`,
  `Documents and Settings`, `Default User` and `All Users` are skipped. The
  first points back at its own parent, so walking it would recurse until
  `MAX_PATH`.
* **`.url` shortcuts** are read as ANSI, UTF-8 or UTF-16, whichever the file
  actually is.
* **A browser that is running.** A live `places.sqlite` can be locked, and the
  newest bookmarks can still be sitting in `places.sqlite-wal`. When either is
  true the database is copied to a temp folder and the copy is read, so Firefox
  is never disturbed and nothing is missed.
* **Console encoding.** Output is forced to UTF-8 with replacement characters, so
  a folder name in Chinese cannot kill a scan on a cp1252 console.

Two limits remain: paths longer than 260 characters are only followed when
long-path support is enabled on the machine, and scan throughput is about 2 MB/s
of bookmark-file content, because the parsers are pure Python.

## GUI

```bash
python3 gui.py --db vault.db --port 8000
# open http://127.0.0.1:8000
```

One page, standard library only, four tabs:

* **Search** — full text over titles, URLs and folders, one row per distinct URL.
* **Duplicates** — pages saved more than once, worst first, each copy showing
  the file it came from.
* **Folders** — folder counts; click one to see its contents.
* **Run** — start a scan, merge, export or split, with the CLI's own live
  progress output.

It is a front end, not a second implementation: every action runs the same
`bmvault.py` command as a subprocess, so behaviour cannot drift. Browsing is
read-only, and nothing is deleted. It binds `127.0.0.1` by default; `--host
0.0.0.0` exposes it, which you only want on a network you trust, because anyone
who can reach the port can start a scan or write files.

## What it reads

| format | detection |
| --- | --- |
| Netscape bookmark HTML | `.html`, `.htm`, or an export from any browser |
| Chrome / Edge / Brave / Chromium JSON | `Bookmarks` (no extension), `.json`, `.bak` |
| Firefox `places.sqlite` | `.sqlite`, `.db`, filename `places.sqlite` |
| CSV / TSV | a column named `url`, `uri`, `link`, or containing "url" |
| Plain text / Markdown | any `http(s)://`, `mailto:` or `magnet:` link |
| Windows `.url` shortcuts | any `.url` file |

Titles, folder paths and added dates are preserved per row. Firefox folder paths
are rebuilt from the bookmark tree; Chrome's `date_added` (microseconds since
1601) and Firefox's (microseconds since 1970) are both converted to ISO dates.

### Whole-disc scans

Point `scan` or `merge` at a drive root and it walks everything, with two rules
that matter for a disc full of old profiles:

* **Hidden folders are searched.** Firefox keeps bookmarks in `~/.mozilla` and
  Chrome in `~/.config/google-chrome` (macOS: `~/Library/Application Support`),
  so skipping dot-folders would miss the two most valuable profiles on the disk.
* **OS and build trees are skipped** — `Windows`, `Program Files`, `ProgramData`,
  `$Recycle.Bin`, `System Volume Information`, `node_modules`, `.git`, `.cache`
  and friends — because walking them wastes minutes and never finds bookmarks.

### Why not every `.html` on the drive

A full drive scan will happily open millions of unrelated documents. By default
a generic extension is only opened when the filename mentions `bookmark`,
`favorit`, `links` or `urls`, or the file sits in a folder whose name does — so
`/home/me/backups/bookmarks/2019.html` is picked up but `/home/me/taxes.html` is
not. `.url` and SQLite files are always examined, because nothing else uses them.
Override with `--any-name`.

## Large files

Bookmark exports get big: a decade of collecting is a 100 MB HTML file, and a
Chrome profile JSON with a million entries runs to hundreds of megabytes. Most
bookmark tools load the whole file into memory (or into a browser tab) and die.
bmvault never holds a file in memory:

* HTML is fed to the parser in 1 MB chunks.
* JSON is tokenised in place, keeping only the stack of open folders; no
  `json.load`, so memory does not grow with file size.
* Text/Markdown is read in chunks and split on newlines, so a minified 300 MB
  dump that is one single line still works.
* Export streams the SQLite cursor and emits folder blocks as the path changes,
  instead of building a tree of every bookmark.

Measured on a synthetic 106 MB corpus (600k Netscape entries, 400k Chrome JSON
entries, 100k links on one line):

```
1,100,000 rows ingested       56s   peak RSS 40 MB
666,973 unique URLs exported  22s   peak RSS 40 MB
```

Matching is regex/token based, so a truncated or partly corrupt file yields the
bookmarks found before the damage instead of failing the whole scan. Folders are
rebuilt from the token stream, including nested ones.

## Splitting

One 60 MB bookmark file is awkward to import, sync or open. `split` cuts the
merged output into complete files of a size you choose:

```bash
python3 bmvault.py split all-bookmarks.html --out-dir parts --max-entries 20000
python3 bmvault.py split --db vault.db --out-dir parts --max-size 5
```

* `--max-entries N` — bookmarks per file.
* `--max-size MB` — start a new file once a part reaches this size. The cut
  happens between bookmarks, so a part can end one entry over the limit.
* `--name` sets the filename stem (`bookmarks-001.html`, `bookmarks-002.html`…),
  `--format` and `--sort` behave as they do for `export`.

With no limit it defaults to 50,000 bookmarks per file. Every part is a complete
Netscape file — its own header, and its folder blocks opened and closed properly —
so any part imports on its own without the others.

## Ordering

`export` and `merge` write one row per distinct URL by default, so the merged
file is already de-duplicated. Entries are sorted, not left in scan order:

| `--sort` | result |
| --- | --- |
| `folder` (default) | folders A→Z, titles A→Z inside each folder, folder structure preserved |
| `title` | one flat list, titles A→Z (untitled entries fall back to their URL) |
| `url` | one flat list, URLs A→Z — good for spotting near-duplicates by eye |
| `added` | one flat list, oldest first |

Only `folder` can rebuild folder blocks; a single global alphabetical run cannot
keep them contiguous without repeating them, so the other sorts are flat.

## How duplicates are decided

The dedupe key (`norm_url`) lowercases the scheme and host, drops a leading
`www.`, default ports, the fragment, trailing slashes, and tracking parameters
(`utm_*`, `fbclid`, `gclid`, `ref`, `mc_cid`, …), then sorts the query string.
`https://Example.com/a/?utm_source=x#top` and `https://example.com/a` are one
bookmark. The original URL is stored untouched in `url` alongside the key.

Deliberately *not* merged: `http` and `https` versions of a page, since those can
be different documents. Flip `MERGE_HTTP_HTTPS = True` in the module if you want
them collapsed.

## Database

```sql
sources  (id, path, format, bytes, mtime, scanned_at, n_bookmarks, note)
bookmarks(id, source_id, url, norm_url, title, folder, added)
```

`bookmarks_fts` is an FTS5 index over title/URL/folder, rebuilt at the end of
each scan; search falls back to `LIKE` if the local SQLite lacks FTS5.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Fixtures cover all six formats, folder reconstruction, date conversion, tracking
parameter stripping, www/slash/fragment collapse, idempotent rescans, FTS search,
and an export-then-reimport round trip. The large-file tests cover multi-chunk
JSON (nested folders across chunk boundaries), a truncated file, a 40,000-link
single-line dump, and streamed export preserving folder nesting.
