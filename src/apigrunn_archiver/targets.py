"""The pages the archiver snapshots on every run.

Copied from ``apigrunn.tracks`` so this package stands alone. The eight track
pages together carry the whole talk archive; the site has no per-talk pages.
"""

from __future__ import annotations

BASE_URL = "https://www.aigrunn.org"

TRACKS: dict[str, str] = {
    "tech": "Tech",
    "business": "Business",
    "government": "Government",
    "education": "Education",
    "sensible-ai": "Sensible AI",
    "science": "Science & Research",
    "healthcare": "Healthcare & Life Sciences",
    "hardware": "Hardware",
}


def track_url(slug: str) -> str:
    """Return the absolute URL of a track page."""
    return f"{BASE_URL}/{slug}"


def default_targets() -> list[str]:
    """Every URL a run archives, in a stable order."""
    return [track_url(slug) for slug in TRACKS]


def resolve(url_or_slug: str) -> str:
    """Accept either a full URL or a track slug such as ``tech``."""
    if "://" in url_or_slug:
        return url_or_slug
    return track_url(url_or_slug.strip("/"))
