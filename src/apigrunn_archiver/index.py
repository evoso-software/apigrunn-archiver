"""The talk index: talks parsed out of the archived HTML.

Pages are parsed as they are fetched, and the result decides whether their
HTML is stored at all (see :mod:`.store`). The parse results are still
derived from the blobs and can be thrown away and rebuilt with
:func:`ensure_index` (``force=True``), or redone for a new parser version.

"The talks in run R" means the talks on every page's latest good, parseable
copy as of R. A page that fails, or whose markup the parser no longer
understands, therefore keeps contributing its last known talks instead of
making them all look deleted; it is reported as *stale* instead.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from .errors import IntegrityError, ParseError
from .models import ArchivedTalk, IndexResult, Talk, TalkChanges
from .parser import PARSER_VERSION, ParsedCard, parse_track_page
from .store import Store

logger = logging.getLogger(__name__)

_CHARSET = re.compile(r"charset=([\w.:-]+)", re.IGNORECASE)


def decode_html(raw: bytes, content_type: str) -> str:
    """Decode a body using the charset the server declared, else UTF-8."""
    match = _CHARSET.search(content_type or "")
    encoding = match.group(1) if match else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def track_slug(url: str) -> str:
    """``https://www.aigrunn.org/tech`` -> ``tech``."""
    return urlsplit(url).path.strip("/") or "index"


def parse_page(raw: bytes, content_type: str, url: str) -> tuple[list[ParsedCard], str | None]:
    """Parse one page body: ``(cards, None)``, or ``([], message)`` on failure."""
    try:
        return parse_track_page(decode_html(raw, content_type), track_slug(url)), None
    except ParseError as exc:
        logger.warning("%s: %s", url, exc)
        return [], exc.message


def ensure_index(store: Store, *, force: bool = False) -> IndexResult:
    """Parse every stored page body not yet parsed by this parser version.

    Parse failures are recorded against the body, not raised.
    """
    if force:
        store.clear_index()

    parsed = 0
    errors: list[tuple[str, str, str]] = []
    for sha, url, content_type in store.unparsed_blobs(PARSER_VERSION):
        try:
            cards, error = parse_page(store.blob(sha), content_type, url)
        except IntegrityError as exc:
            logger.warning("%s (%s): %s", url, sha[:12], exc)
            cards, error = [], exc.message
        if error is not None:
            errors.append((url, sha, error))
        store.save_parse(sha, url, parser_version=PARSER_VERSION, cards=cards, error=error)
        parsed += 1
    return IndexResult(parsed=parsed, errors=errors)


def page_state(store: Store, run_id: int) -> tuple[dict[str, tuple[int, str]], list[str]]:
    """Each page's latest good, parsed version as of ``run_id``, plus stale pages.

    Returns ``({url: (run_id_used, page_sha256)}, stale_urls)``, where a page
    is stale if its snapshot in ``run_id`` itself was not usable.
    """
    state: dict[str, tuple[int, str]] = {}
    for rid, url, page, error in store.parsed_snapshots():
        if rid > run_id:
            break
        if error is None and page is not None:
            state[url] = (rid, page)
    stale = sorted(
        snap.url
        for snap in store.snapshots(run_id)
        if state.get(snap.url, (None, None))[0] != run_id
    )
    return state, stale


def _talk(row, tracks: tuple[str, ...]) -> Talk:
    return Talk(
        video_id=row["video_id"],
        title=row["title"],
        speaker=row["speaker"],
        description=row["description"],
        url=row["talk_url"],
        year=row["year"],
        tracks=tracks,
        thumbnail_url=row["thumbnail_url"],
    )


def page_talks(store: Store, page_sha256: str, url: str) -> dict[str, Talk]:
    """The talks on one page version, keyed by video id."""
    slug = (track_slug(url),)
    return {row["video_id"]: _talk(row, slug) for row in store.cards(page_sha256)}


def talks_in_run(store: Store, run_id: int) -> dict[str, Talk]:
    """The merged talk archive as of ``run_id``, keyed by video id."""
    state, _ = page_state(store, run_id)
    tracks: dict[str, set[str]] = {}
    best: dict[str, object] = {}
    for url in sorted(state):
        _, page = state[url]
        slug = track_slug(url)
        for row in store.cards(page):
            vid = row["video_id"]
            tracks.setdefault(vid, set()).add(slug)
            current = best.get(vid)
            # Prefer a card that knows the year; only the lane headers carry it.
            if current is None or (current["year"] is None and row["year"] is not None):
                best[vid] = row

    return {vid: _talk(row, tuple(sorted(tracks[vid]))) for vid, row in best.items()}


def sort_talks(talks) -> list[Talk]:
    return sorted(talks, key=lambda t: (-(t.year or 0), t.title.casefold(), t.video_id))


def talk_changes(store: Store, from_run: int, to_run: int) -> TalkChanges:
    before = talks_in_run(store, from_run)
    after = talks_in_run(store, to_run)
    _, stale = page_state(store, to_run)
    return TalkChanges(
        from_run=from_run,
        to_run=to_run,
        added=sort_talks(after[v] for v in after.keys() - before.keys()),
        removed=sort_talks(before[v] for v in before.keys() - after.keys()),
        changed=sorted(
            ((before[v], after[v]) for v in before.keys() & after.keys() if before[v] != after[v]),
            key=lambda pair: (-(pair[1].year or 0), pair[1].title.casefold()),
        ),
        stale_pages=stale,
    )


def archived_talks(store: Store) -> list[ArchivedTalk]:
    """Every talk ever seen, with first/last sighting and current presence."""
    latest = store.latest_run_id()
    if latest is None:
        return []
    current = talks_in_run(store, latest)
    by_run: dict[int, dict[str, Talk]] = {latest: current}

    out: list[ArchivedTalk] = []
    for row in store.sightings():
        vid, last = row["video_id"], row["last_run"]
        if vid in current:
            talk = current[vid]
        else:
            if last not in by_run:
                by_run[last] = talks_in_run(store, last)
            talk = by_run[last][vid]
        out.append(
            ArchivedTalk(
                talk=talk,
                first_seen_run=row["first_run"],
                last_seen_run=last,
                present=vid in current,
            )
        )
    out.sort(key=lambda a: (-(a.talk.year or 0), a.talk.title.casefold(), a.talk.video_id))
    return out
