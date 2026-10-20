# apigrunn-archiver

A versioned local archive of the [aiGrunn](https://www.aigrunn.org/) website.

[apigrunn](../apigrunn) keeps a *cache*: it mirrors the site and prunes whatever the
site removes. This package keeps an *archive*. Every run is stored, the HTML of
each page is kept byte for byte, and nothing is ever deleted. If a talk or a whole
page disappears from aigrunn.org, your local copy still has it.

Currently archived: the eight track pages, which together carry the full talk archive.
No images or CSS.

## Usage

```bash
apigrunn-archiver run                     # snapshot every page (exit 1 if any page failed)
apigrunn-archiver run --conditional       # trust ETags; unchanged pages cost a 304
apigrunn-archiver runs                    # list runs, newest first
apigrunn-archiver show tech               # latest good copy of /tech
apigrunn-archiver show tech --at 2026-03-01   # as it was by the end of that day (UTC)
apigrunn-archiver show tech --run 4 --raw > tech.html   # exact bytes from run 4
```

```python
from apigrunn_archiver import Archiver

with Archiver() as archiver:
    print(archiver.run())        # run 3: ok — 8 pages, 1 changed, 1 new blobs, 0 errors
    archiver.runs()              # [Run(...), ...]
    archiver.snapshots(3)        # what each page looked like in run 3
    archiver.html("tech", at="2026-03-01")
    archiver.content("https://www.aigrunn.org/tech", run_id=1)   # raw bytes
```

Pages can be named by track slug (`tech`) or full URL.

## How it stores things

One SQLite file, by default `$XDG_DATA_HOME/apigrunn-archiver/archive.db`
(`~/.local/share/...`), overridable with `$APIGRUNN_ARCHIVE` or `--db`. It lives
under *data*, not *cache*, so tools that clean `~/.cache` leave it alone.

```sql
runs      (id, started_at, finished_at, tool_version, status)  -- ok | partial | failed | running
snapshots (run_id, url, final_url, fetched_at, status_code, error, headers,
           etag, last_modified, blob_sha256, changed)          -- one per (run, url)
blobs     (sha256, size, compression, data)                    -- gzip'd page bodies
```

- **Blobs are content-addressed.** A page that hasn't changed adds a snapshot row that
  points at the blob it already has, so daily runs of an unchanged site are nearly free.
  `changed` marks the snapshots whose body differs from the previous good one.
- **Failures are data.** A 404, a 503 or a timeout is stored as a snapshot, along with
  the error body if there is one. Reads skip error snapshots, so `show tech` still
  returns the last good copy after the page is gone.
- **Full downloads by default.** Every page is downloaded in full and deduplicated by
  hash, so nothing depends on the server's ETags being correct. `--conditional` sends
  `If-None-Match`/`If-Modified-Since` instead; a `304` reuses the previous blob.
- **Bodies are checked on read.** Every blob is verified against its SHA-256 when it is
  read and raises `IntegrityError` if it doesn't match.
- **The schema migrates forward** (`PRAGMA user_version`). Only an archive written by a
  *newer* version is refused, and that file is left untouched.

To back up while a run might be in progress, use `sqlite3 archive.db ".backup copy.db"`
instead of `cp`.

## Errors

| Code | Exception | Raised when |
| --- | --- | --- |
| `ARCHIVER-E300` | `SchemaVersionError` | the archive is from a newer version |
| `ARCHIVER-E400` | `SnapshotNotFound` | no snapshot matches the URL / time / run |
| `ARCHIVER-E500` | `IntegrityError` | a stored blob doesn't match its hash |

All inherit from `ArchiverError`. Network and HTTP failures during `run()` never raise.

## Scheduling

```cron
0 6 * * *  apigrunn-archiver run
```

## Development

```bash
nix develop -c uv sync
nix develop -c uv run pytest              # offline, against tests/fixtures
nix develop -c uv run pytest -m network   # one real run against aigrunn.org
```

## Courtesy

`robots.txt` on aigrunn.org is `Allow: /`. The archiver sends a descriptive
`User-Agent` and fetches pages one at a time: 8 requests per run.

## Licence

MIT
