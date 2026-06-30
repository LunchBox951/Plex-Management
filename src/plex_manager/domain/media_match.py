"""Media identity gate — does a parsed release actually name the wanted title?

Prowlarr can return releases for a *different* movie/show than the one searched
for: an indexer that ignores the ``tmdbid`` param, a stale id->title mapping, or a
loosely-matched text fallback. The decision engine only gates on quality +
blocklist, so without this check a high-quality WRONG release could become the top
accepted result and be grabbed — a direct north-star violation ("do NOT grab the
wrong media").

:func:`matches_media` is the conservative identity test the engine runs FIRST,
before the quality gate. It prefers a definitive id match; failing that it falls
back to a normalized title + year comparison and, when still uncertain, REJECTS
rather than risk grabbing the wrong thing.

Pure domain: stdlib + the local release model only.
"""

from __future__ import annotations

import re
import unicodedata

from plex_manager.domain.release import ParsedRelease

__all__ = ["matches_media", "normalize_title"]

# Collapse a title to a comparison key: casefold, fold diacritics to their base
# ASCII letter, then drop everything that is not an ASCII letter or digit
# (punctuation, separators, whitespace). So "Spider-Man: No Way Home" and
# "spider man no way home" compare equal, and the accented TMDB title "Amélie"
# matches the ASCII scene/p2p release "Amelie".
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Releases routinely name a movie a year off from the metadata's release year
# (festival vs. wide release, regional dates), so an exact year match is too
# strict; a +/-1 window is the accepted tolerance.
_YEAR_TOLERANCE = 1


def normalize_title(title: str) -> str:
    """Return the punctuation/whitespace-insensitive, casefolded comparison key.

    Diacritics are folded to their base ASCII letter (NFKD-decompose, drop
    combining marks) *before* stripping, so an accented metadata title and its
    ASCII release name compare equal instead of normalizing to different keys.
    """
    folded = unicodedata.normalize("NFKD", title.casefold())
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return _NON_ALNUM.sub("", stripped)


def matches_media(
    parsed: ParsedRelease,
    expected_title: str,
    expected_year: int | None,
    candidate_tmdb_id: int,
    expected_tmdb_id: int,
) -> bool:
    """Return ``True`` only when ``parsed`` plausibly names the wanted media.

    Decision order, conservative by design:

    1. **Authoritative id match.** When the candidate carries a non-zero
       ``tmdb_id`` *and* the request has one, the id is decisive — equal accepts,
       unequal rejects (no title fallback can override a definitive id mismatch).
    2. **Title + year fallback.** Otherwise compare the parsed title to the
       expected title under :func:`normalize_title`; a mismatch rejects. The year
       must then match within +/-1, unless the request carries no year (in which
       case the title match alone suffices). A title match with an expected year
       but *no* parsed year is treated as uncertain and rejected — better to
       surface ``no_acceptable_release`` than grab the wrong thing.
    """
    if candidate_tmdb_id and expected_tmdb_id:
        return candidate_tmdb_id == expected_tmdb_id

    expected_key = normalize_title(expected_title)
    if not expected_key or normalize_title(parsed.clean_title) != expected_key:
        return False

    if expected_year is None:
        return True
    if parsed.year is None:
        return False
    return abs(parsed.year - expected_year) <= _YEAR_TOLERANCE
