# apigrunn-archiver

A versioned local archive of the [aiGrunn](https://www.aigrunn.org/) website.

[apigrunn](../apigrunn) keeps a *cache*: it mirrors the site and prunes whatever the
site removes. This package keeps an *archive*. Every run is stored, the HTML of
each page is kept byte for byte, and nothing is ever deleted. If a talk or a whole
page disappears from aigrunn.org, your local copy still has it.

Currently archived: the eight track pages, which together carry the full talk archive.
No images or CSS. The talks are parsed out of the stored HTML into an index, so you can
ask which talks the site dropped and when.

## Usage

```bash
apigrunn-archiver run                     # snapshot every page, report talk changes
apigrunn-archiver run --conditional       # trust ETags; unchanged pages cost a 304
apigrunn-archiver runs                    # list runs, newest first
apigrunn-archiver show tech               # latest good copy of /tech
apigrunn-archiver show tech --at 2026-03-01   # as it was by the end of that day (UTC)
apigrunn-archiver show tech --run 4 --raw > tech.html   # exact bytes from run 4

apigrunn-archiver history tech            # when /tech changed, failed or recovered
apigrunn-archiver diff tech 3 7           # unified diff between runs (exit 1 if different)
apigrunn-archiver diff tech 3             # ... against the latest good copy
apigrunn-archiver verify                  # re-hash every stored page (exit 1 if damaged)
apigrunn-archiver export ~/aigrunn-export --all   # plain .html files, one dir per run

apigrunn-archiver talks                   # the talk archive as of the latest run
apigrunn-archiver talks --run 4 --year 2024 --track healthcare --search RAG
apigrunn-archiver talks --gone            # talks the site used to list but no longer does
apigrunn-archiver talks --ever            # every talk ever seen
apigrunn-archiver changes 3 7             # talks added / removed / edited between runs
apigrunn-archiver reindex [--force]       # re-parse stored HTML into the talk index
```

`history` lists only transitions, i.e. the first copy, each change of content, the
start of a run of failures and the recovery from one:

```
run    1  2026-03-01T06:00:02+00:00  first               56bd5e84eaaa
run   14  2026-03-14T06:00:01+00:00  changed             a0c3f19e7d21
run   30  2026-03-30T06:00:03+00:00  failed              HTTP 404
run   31  2026-03-31T06:00:02+00:00  recovered           a0c3f19e7d21
```

Pass `--all` to see every snapshot.

```python
from apigrunn_archiver import Archiver

with Archiver() as archiver:
    print(archiver.run())        # run 3: ok — 8 pages, 1 changed, 1 new blobs, 0 errors
    archiver.runs()              # [Run(...), ...]
    archiver.snapshots(3)        # what each page looked like in run 3
    archiver.html("tech", at="2026-03-01")
    archiver.content("https://www.aigrunn.org/tech", run_id=1)   # raw bytes
    archiver.history("tech")     # [HistoryEntry(kind="first", snapshot=...), ...]
    archiver.diff("tech", 3, 7)  # unified diff as a str, "" if identical
    archiver.verify()            # VerifyResult(ok=..., bad_blobs=[...], ...)
    archiver.export("out/", all_runs=True)

    archiver.talks(year=2024, track="healthcare")   # [Talk(...), ...]
    archiver.talks(run_id=4)                        # as the site listed them in run 4
    archiver.talk_changes(3, 7)                     # TalkChanges(added=, removed=, changed=, ...)
    archiver.archived_talks(gone_only=True)         # [ArchivedTalk(talk=, last_seen_run=, ...)]
```

## The talk index

`run` parses every page whose HTML changed and prints how the talk archive moved since
the previous run:

```
run 8: ok — 8 pages, 2 changed, 2 new blobs, 0 errors
  changed  https://www.aigrunn.org/tech
  changed  https://www.aigrunn.org/science
talks run 7 → 8: +0 added, -1 removed, ~0 changed
  - [2025] We Put YOLO on a Tractor — What could go wrong? — Wieneke Keller & Navaneeth Krishnan
```

How it behaves:

- **Talks in a run** means the talks on each page's latest *good* copy as of that run. If
  `/tech` returns 503 in run 8, its talks are taken from run 7 and `/tech` is reported as
  **stale**. So a hiccup doesn't look like 66 deleted talks.
- **Removed vs. changed.** A talk that disappears from every page is *removed*. A talk
  that loses one track, or whose title, speaker or description is edited, is *changed*.
- **Markup changes don't lose data.** If the parser can't read a page (`ARCHIVER-E200`),
  the run still archives the HTML. The failure is recorded and the page counts as stale.
  Once the parser is fixed, `reindex` re-reads the stored HTML.
- **Parsing is cheap.** Results are stored per distinct page version, not per run, so
  a year of daily runs on an unchanged site is parsed exactly 8 times. Pages parsed by an
  older `PARSER_VERSION` are re-parsed automatically. `reindex --force` rebuilds the
  whole index.

The index lives in its own two tables (`parsed_pages`, `talk_cards`). It is derived
entirely from `blobs`, so dropping it loses nothing.

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
  read and raises `IntegrityError` if it doesn't match. `verify` checks all of them.
- **The schema migrates forward** (`PRAGMA user_version`). Only an archive written by a
  *newer* version is refused, and that file is left untouched.

## Export

`export` writes an archive copy that needs neither this package nor SQLite:

```
export/
  run-0007/
    manifest.json             # the run, plus every snapshot with status, headers, sha256
    www.aigrunn.org/
      tech.html               # exact bytes as served; mtime = fetch time
      business.html
      ...
```

Failed snapshots appear in `manifest.json` with `"file": null` and have no file written.
Defaults to the latest run; `--run N` exports one run and `--all` exports every run.

## Backups

Run `verify` from time to time: it re-hashes every blob, checks that every snapshot's
blob exists, and runs SQLite's `integrity_check`.

To back up while a run might be in progress, use `sqlite3 archive.db ".backup copy.db"`
instead of `cp`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | `run`: a page failed · `diff`: pages differ · `verify`: damage found · `reindex`: a page could not be parsed |
| 2 | bad arguments, or nothing matches (e.g. no such run) |
| 3 | `run`: talks disappeared from the site since the previous run |

With cron, any non-zero exit code means you get an email, so a deleted talk is
reported the morning after it happens.

## Errors

| Code | Exception | Raised when |
| --- | --- | --- |
| `ARCHIVER-E200` | `ParseError` | a page has talk cards but none could be extracted (recorded, not raised, during runs) |
| `ARCHIVER-E300` | `SchemaVersionError` | the archive is from a newer version |
| `ARCHIVER-E400` | `SnapshotNotFound` | no snapshot matches the URL / time / run |
| `ARCHIVER-E500` | `IntegrityError` | a stored blob doesn't match its hash or won't decompress |

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
