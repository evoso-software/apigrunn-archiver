# apigrunn-archiver

A versioned local archive of the [aiGrunn](https://www.aigrunn.org/) website.

[apigrunn](../apigrunn) keeps a *cache*: it mirrors the site and prunes whatever the
site removes. This package keeps an *archive*. Every run is recorded and nothing is ever
deleted. If a talk or a whole page disappears from aigrunn.org, your local copy still
has it.

Currently archived: the eight track pages, which together carry the full talk archive.
No images or CSS. Each page is parsed into its talks as it is fetched, and a page only
counts as changed when its talks change. Its HTML is kept for every version of the talks,
so the parser can always re-read it, but a run in which the site only changed a nonce or
a build hash adds nothing but a row per page.

## Usage

```bash
apigrunn-archiver run                     # snapshot every page, report talk changes
apigrunn-archiver run --conditional       # trust ETags; unchanged pages cost a 304
apigrunn-archiver runs                    # list runs, newest first
apigrunn-archiver show tech               # latest good copy of /tech
apigrunn-archiver show tech --at 2026-03-01   # as it was by the end of that day (UTC)
apigrunn-archiver show tech --run 4 --raw > tech.html   # the HTML stored for run 4

apigrunn-archiver history tech            # when /tech's talks changed, or it failed or recovered
apigrunn-archiver diff tech 3 7           # talks added/removed/edited on /tech (exit 1 if any)
apigrunn-archiver diff tech 3             # ... against the latest good copy
apigrunn-archiver diff tech 3 7 --html    # unified diff of the stored HTML
apigrunn-archiver verify                  # re-hash every stored page (exit 1 if damaged)
apigrunn-archiver export ~/aigrunn-export --all   # plain .html files, one dir per run

apigrunn-archiver talks                   # the talk archive as of the latest run
apigrunn-archiver talks --run 4 --year 2024 --track healthcare --search RAG
apigrunn-archiver talks --gone            # talks the site used to list but no longer does
apigrunn-archiver talks --ever            # every talk ever seen
apigrunn-archiver changes 3 7             # talks added / removed / edited between runs
apigrunn-archiver reindex [--force]       # re-parse stored HTML into the talk index
```

`history` lists only transitions, i.e. the first copy, each change of talks, the
start of a run of failures and the recovery from one. The hash identifies the page
version (the talks), not the bytes:

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
    archiver.content("https://www.aigrunn.org/tech", run_id=1)   # stored HTML bytes
    archiver.history("tech")     # [HistoryEntry(kind="first", snapshot=...), ...]
    archiver.diff("tech", 3, 7)  # talk changes as a str, "" if none; html=True for HTML
    archiver.verify()            # VerifyResult(ok=..., bad_blobs=[...], ...)
    archiver.export("out/", all_runs=True)

    archiver.talks(year=2024, track="healthcare")   # [Talk(...), ...]
    archiver.talks(run_id=4)                        # as the site listed them in run 4
    archiver.talk_changes(3, 7)                     # TalkChanges(added=, removed=, changed=, ...)
    archiver.archived_talks(gone_only=True)         # [ArchivedTalk(talk=, last_seen_run=, ...)]
```

To follow a run as it happens, pass `on_snapshot`. It is called with each page's
`Snapshot` as soon as it is stored, in target order, failed pages included:

```python
archiver.run(on_snapshot=lambda s: print(s.url, "ok" if s.ok else s.error))
```

Each snapshot is committed before the callback runs, so another `Archiver` on the same
file (in another thread or process) can already read it. To cancel a run, raise from the
callback: the pages stored so far are kept, the run is marked `failed` and the exception
propagates. The callback runs on the thread that called `run()`.

## The talk index

`run` parses every page as it is fetched and prints how the talk archive moved since
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
  the run always stores its HTML. The failure is recorded and the page counts as stale.
  Once the parser is fixed, `reindex` re-reads the stored HTML.
- **A parser upgrade re-reads stored HTML.** Blobs parsed by an older `PARSER_VERSION`
  are re-parsed automatically, and `changed`/`history` follow. `reindex --force` rebuilds
  the whole index from the blobs.

The talk and page versions are derived entirely from `blobs`, so `reindex --force` can
always rebuild them.

Pages can be named by track slug (`tech`) or full URL.

## How it stores things

One SQLite file, by default `$XDG_DATA_HOME/apigrunn-archiver/archive.db`
(`~/.local/share/...`), overridable with `$APIGRUNN_ARCHIVE` or `--db`. It lives
under *data*, not *cache*, so tools that clean `~/.cache` leave it alone.

```sql
runs          (id, started_at, finished_at, tool_version, status)  -- ok | partial | failed | running
snapshots     (run_id, url, final_url, fetched_at, status_code, error, headers,
               etag, last_modified, body_sha256, blob_sha256)      -- one per (run, url)
talk_versions (sha256, video_id, title, speaker, description, url, year, thumbnail_url)
page_versions (sha256, cards)                                      -- an ordered list of talks
page_talks    (page_sha256, position, talk_sha256)
blob_parses   (blob_sha256, url, parser_version, page_sha256, error)
blobs         (sha256, size, compression, data)                    -- gzip'd page bodies
```

- **Talks decide what changed.** A talk version is one distinct set of talk fields, keyed
  by their hash, and a page version is the ordered list of talk versions on a page. Both
  are written once and shared, so a talk listed on four tracks is one row. The view
  `snapshot_states` adds each snapshot's page version and `changed`, which is computed
  rather than stored, so a parser upgrade corrects it everywhere.
- **HTML is stored once per page version.** A page whose talks match a copy already
  stored for that URL adds only a snapshot row, which points at that copy. `blob_sha256`
  is therefore *the HTML that stands for the snapshot*: same talks, but not necessarily
  the bytes received. `body_sha256` is the hash of those, and `Snapshot.exact` says
  whether the two match; `show` prints a note on stderr when they don't.
- **What you give up.** Runs that differed only in noise keep no HTML of their own. If
  the site adds something the parser does not extract (a new field, a talk in markup it
  skips), a later parser fix can only re-read the copies that were stored.
- **Failures are data.** A 404, a 503 or a timeout is stored as a snapshot with its
  status and error, but not its body. Reads skip error snapshots, so `show tech` still
  returns the last good copy after the page is gone.
- **Full downloads by default.** Every page is downloaded in full and parsed, so nothing
  depends on the server's ETags being correct. `--conditional` sends
  `If-None-Match`/`If-Modified-Since` instead; a `304` reuses the previous blob.
- **Everything is checked.** Every blob is verified against its SHA-256 when it is read
  and raises `IntegrityError` if it doesn't match. `verify` checks all blobs and also
  re-hashes every talk and page version.
- **No migrations while at 0.x.** The schema version is in `PRAGMA user_version`. An
  archive written for any other schema is refused and left untouched; move it aside to
  start a new one.

## Export

`export` writes an archive copy that needs neither this package nor SQLite:

```
export/
  run-0007/
    manifest.json             # the run, plus every snapshot with status, headers, hashes
    www.aigrunn.org/
      tech.html               # the stored HTML; mtime = fetch time
      business.html
      ...
```

Failed snapshots appear in `manifest.json` with `"file": null` and have no file written.
`"exact": false` marks a file that is an earlier copy with the same talks rather than the
bytes served in that run.
Defaults to the latest run; `--run N` exports one run and `--all` exports every run.

## Backups

Run `verify` from time to time: it re-hashes every blob and every talk and page version,
checks that every snapshot's blob exists, and runs SQLite's `integrity_check`.

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
| `ARCHIVER-E300` | `SchemaVersionError` | the archive is from another schema version |
| `ARCHIVER-E400` | `SnapshotNotFound` | no snapshot matches the URL / time / run |
| `ARCHIVER-E500` | `IntegrityError` | a stored blob doesn't match its hash or won't decompress |

All inherit from `ArchiverError`. Network and HTTP failures during `run()` never raise;
an exception raised by an `on_snapshot` callback does.

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
