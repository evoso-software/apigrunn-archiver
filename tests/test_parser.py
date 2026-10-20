"""The parser is the part coupled to someone else's markup, so it gets the
most detailed tests — all against snapshots taken on 2026-09-17.

Copied from apigrunn along with the parser itself."""

from __future__ import annotations

import pytest

from apigrunn_archiver.errors import ParseError
from apigrunn_archiver.parser import parse_track_page, youtube_video_id
from apigrunn_archiver.targets import TRACKS

from .conftest import fixture_html

# Counts verified against the live site on 2026-09-17.
EXPECTED_CARDS = {
    "tech": 66,
    "business": 19,
    "government": 16,
    "education": 8,
    "sensible-ai": 13,
    "science": 9,
    "healthcare": 6,
    "hardware": 8,
}
# The site's own "N talks" badges are hand-written and not always accurate:
# the 2024 badge claims 28 while the lane actually holds 27 cards. We count
# cards, not badges.
EXPECTED_TECH_BY_YEAR = {2025: 22, 2024: 27, 2023: 17}


@pytest.mark.parametrize(("track", "count"), EXPECTED_CARDS.items())
def test_card_counts_per_track(track: str, count: int) -> None:
    assert len(parse_track_page(fixture_html(track), track)) == count


def test_tech_talks_grouped_by_year(tech_html: str) -> None:
    cards = parse_track_page(tech_html, "tech")
    by_year: dict[int | None, int] = {}
    for card in cards:
        by_year[card.year] = by_year.get(card.year, 0) + 1
    assert by_year == EXPECTED_TECH_BY_YEAR


def test_tech_page_is_a_superset_of_every_other_track() -> None:
    tech_ids = {c.video_id for c in parse_track_page(fixture_html("tech"), "tech")}
    union = set()
    for track in TRACKS:
        union |= {c.video_id for c in parse_track_page(fixture_html(track), track)}
    assert union == tech_ids
    assert len(union) == 66


def test_tracks_are_many_to_many() -> None:
    tracks_by_video: dict[str, set[str]] = {}
    for track in TRACKS:
        for card in parse_track_page(fixture_html(track), track):
            tracks_by_video.setdefault(card.video_id, set()).add(track)
    # This talk is listed on four track pages.
    assert tracks_by_video["ys__Y1mICwo"] == {"tech", "government", "education", "sensible-ai"}
    assert sum(len(v) for v in tracks_by_video.values()) == 145


def test_card_fields_are_complete(tech_html: str) -> None:
    card = next(c for c in parse_track_page(tech_html, "tech") if c.video_id == "tP_kg20VAho")
    assert card.title == "We Put YOLO on a Tractor — What could go wrong?"
    assert card.speaker == "Wieneke Keller & Navaneeth Krishnan"  # &amp; decoded
    assert card.description.startswith("Aurea Imaging retrofits tractors")
    assert card.url == "https://www.youtube.com/watch?v=tP_kg20VAho"
    assert card.year == 2025
    assert card.thumbnail_url == "https://img.youtube.com/vi/tP_kg20VAho/mqdefault.jpg"
    assert card.track == "tech"


def test_descriptions_are_not_truncated(tech_html: str) -> None:
    cards = parse_track_page(tech_html, "tech")
    assert all(c.description for c in cards)
    assert not [c for c in cards if c.description.rstrip().endswith(("...", "…"))]


def test_missing_speaker_is_empty_not_an_error(tech_html: str) -> None:
    """One card has `data-speaker=""` next to a visible `aiGrunn 2024` label.

    The label is a year, not a speaker, so the empty attribute must win.
    """
    cards = parse_track_page(tech_html, "tech")
    blank = [c for c in cards if not c.speaker]
    assert len(blank) == 1
    assert blank[0].title == "Designing Human-AI Interaction for Effective Assistance"
    assert "aiGrunn" not in {c.speaker for c in cards}


def test_visible_text_is_used_when_the_attribute_is_absent() -> None:
    html = (
        '<div class="talks-year-group"><span class="talks-year-badge y2025">2025</span>'
        '<a class="talk-card" href="https://youtu.be/aaaaaaaaaaa">'
        '<div class="talk-title">Rendered title</div>'
        '<div class="talk-speaker">Rendered speaker</div></a></div>'
    )
    card = parse_track_page(html, "tech")[0]
    assert card.title == "Rendered title"
    assert card.speaker == "Rendered speaker"


def test_no_duplicate_video_ids_within_a_page(tech_html: str) -> None:
    cards = parse_track_page(tech_html, "tech")
    assert len({c.video_id for c in cards}) == len(cards)


def test_page_without_an_archive_returns_nothing() -> None:
    assert parse_track_page("<html><body><h1>Soon</h1></body></html>", "tech") == []


def test_changed_markup_raises_parse_error() -> None:
    html = '<div class="talks-year-group"><a class="talk-card" href="/nope"></a></div>'
    with pytest.raises(ParseError, match=r"ARCHIVER-E200.*markup has probably changed"):
        parse_track_page(html, "tech")


def test_cards_outside_a_year_group_are_reported(caplog: pytest.LogCaptureFixture) -> None:
    html = (
        '<div class="talks-year-group"><span class="talks-year-badge y2025">2025</span>'
        '<a class="talk-card" href="https://youtu.be/aaaaaaaaaaa" data-title="In a group"></a>'
        "</div>"
        '<a class="talk-card" href="https://youtu.be/bbbbbbbbbbb" data-title="Orphan"></a>'
    )
    with caplog.at_level("WARNING"):
        cards = parse_track_page(html, "tech")
    assert [c.title for c in cards] == ["In a group"]
    assert "only 1 inside a year group" in caplog.text


def test_year_falls_back_to_badge_text() -> None:
    html = (
        '<div class="talks-year-group"><span class="talks-year-badge">2019</span>'
        '<a class="talk-card" href="https://www.youtube.com/watch?v=xyz" data-title="T"></a></div>'
    )
    assert parse_track_page(html, "tech")[0].year == 2019


def test_year_missing_entirely_is_none() -> None:
    html = (
        '<div class="talks-year-group">'
        '<a class="talk-card" href="https://www.youtube.com/watch?v=xyz" data-title="T"></a></div>'
    )
    assert parse_track_page(html, "tech")[0].year is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc123", "abc123"),
        ("https://youtube.com/watch?v=abc123&t=10", "abc123"),
        ("https://youtu.be/abc123", "abc123"),
        ("https://www.youtube.com/embed/abc123", "abc123"),
        ("https://www.youtube.com/shorts/abc123", "abc123"),
        ("https://example.com/video", None),
        ("https://www.youtube.com/watch", None),
        ("", None),
    ],
)
def test_youtube_video_id(url: str, expected: str | None) -> None:
    assert youtube_video_id(url) == expected

