"""Unit table for the pure media-identity gate (``matches_media``).

Covers the decision order: authoritative id match wins; otherwise a conservative
normalized title + year fallback that REJECTS when uncertain (no id, loose title,
or a missing year on a year-scoped request).
"""

from __future__ import annotations

import pytest

from plex_manager.domain.media_match import matches_media, normalize_title
from plex_manager.domain.release import ParsedRelease


def _parsed(title: str, year: int | None = None) -> ParsedRelease:
    return ParsedRelease(raw_title=title, clean_title=title, year=year)


@pytest.mark.parametrize(
    ("parsed", "expected_title", "expected_year", "candidate_tmdb", "expected_tmdb", "want"),
    [
        # --- authoritative id match: decisive, title is irrelevant -------------
        # Equal non-zero tmdb ids accept even when the parsed title is garbage.
        (_parsed("Totally Different Name", 1999), "Inception", 2010, 27205, 27205, True),
        # Unequal non-zero tmdb ids reject even when the title matches perfectly.
        (_parsed("Inception", 2010), "Inception", 2010, 11111, 27205, False),
        # --- title + year fallback (no usable id on candidate) -----------------
        # Exact title + year accept.
        (_parsed("Inception", 2010), "Inception", 2010, 0, 27205, True),
        # Punctuation/casing/spacing differences still match (normalized).
        (_parsed("spider-man no way home", 2021), "Spider-Man: No Way Home", 2021, 0, 0, True),
        # Year within the +/-1 tolerance accepts (festival vs. wide release).
        (_parsed("Inception", 2011), "Inception", 2010, 0, 0, True),
        # Year outside the tolerance rejects.
        (_parsed("Inception", 2008), "Inception", 2010, 0, 0, False),
        # Different title rejects regardless of year.
        (_parsed("Interstellar", 2010), "Inception", 2010, 0, 0, False),
        # Request has no year -> a title match alone suffices.
        (_parsed("Inception", 2010), "Inception", None, 0, 0, True),
        # Conservative: title matches but the release carries NO year while the
        # request specifies one -> uncertain -> reject (don't grab the wrong year).
        (_parsed("Inception", None), "Inception", 2010, 0, 0, False),
        # Candidate has a tmdb id but the request does not -> fall back to title.
        (_parsed("Inception", 2010), "Inception", 2010, 27205, 0, True),
        # Accented metadata title vs. ASCII release: diacritics are folded, so the
        # title fallback matches instead of rejecting a correct release WRONG_MEDIA.
        (_parsed("Amelie", 2001), "Amélie", 2001, 0, 0, True),
        (_parsed("Leon The Professional", 1994), "Léon: The Professional", 1994, 0, 0, True),
        (_parsed("Pokemon Detective Pikachu", 2019), "Pokémon Detective Pikachu", 2019, 0, 0, True),
        # Folding does not collapse genuinely different titles.
        (_parsed("It Follows", 2014), "It", 2017, 0, 0, False),
        # Empty expected title can never match (no oracle to compare against).
        (_parsed("Inception", 2010), "", None, 0, 0, False),
    ],
)
def test_matches_media_table(
    parsed: ParsedRelease,
    expected_title: str,
    expected_year: int | None,
    candidate_tmdb: int,
    expected_tmdb: int,
    want: bool,
) -> None:
    assert (
        matches_media(
            parsed,
            expected_title=expected_title,
            expected_year=expected_year,
            candidate_tmdb_id=candidate_tmdb,
            expected_tmdb_id=expected_tmdb,
        )
        is want
    )


def test_normalize_title_strips_punctuation_and_case() -> None:
    assert normalize_title("Spider-Man: No Way Home") == "spidermannowayhome"
    assert normalize_title("  The.Matrix  ") == "thematrix"
    assert normalize_title("WALL·E") == "walle"


def test_normalize_title_folds_diacritics_to_ascii() -> None:
    # Accented metadata titles fold to the same key as their ASCII release names.
    assert normalize_title("Amélie") == normalize_title("Amelie") == "amelie"
    assert normalize_title("Léon: The Professional") == "leontheprofessional"
    assert normalize_title("Pokémon Detective Pikachu") == normalize_title(
        "Pokemon Detective Pikachu"
    )
    assert normalize_title("Cidade de Deus") == "cidadededeus"
