# ADR-0008: `guessit` is the release parser; the quality model stays ours

- **Status:** Accepted — 2026-06-29
- **Deciders:** LunchBox951 (owner)
- **Resolves:** open question §11.2 in [`docs/design/overview.md`](../design/overview.md)

## Context

[ADR-0001](0001-integrated-app-borrowed-brains.md) commits us to *borrowing
proven brains* for release parsing rather than re-deriving it (the prototype's
homegrown `scoring.py` / `torrent_validator.py` is exactly what let CAM/TELESYNC
releases through). The design parked the **choice of parser library** for the v1
session, listing three candidates: `guessit`, `parse-torrent-title` (PTN), and
`RTN` (rank-torrent-name, which parses *and* ranks).

The decision interacts with our quality model: v2 ranks releases with a
**Radarr-style ordered quality profile and a hard cutoff** that rejects
CAM/TS/TELECINE *categorically* — not as a low score. A library that also ranks
(RTN) would introduce a *second*, additive notion of "better" that competes with
our ordered profile, reopening the fuzzy-selection class of bug ADR-0001 closes.

## Decision

Use **`guessit` as a parse-only library** behind a pure `ParserPort`, and keep
**100% of ranking, scoring, and the hard cutoff in the domain core.**

- `guessit` turns a raw release name into fields; its `source` taxonomy already
  emits `Camera` / `Telesync` / `HD Telesync` / `Telecine` / `Workprint` and an
  `other: Screener` flag as first-class values. The adapter maps these into our
  own `QualitySource` / `Modifier` enums (a port of Radarr's `Quality.cs`
  taxonomy and weight table).
- The borrowed parser **never crosses the hexagon with a ranking opinion**:
  `ParserPort` exposes parsed *fields* only, never a score. The decision engine
  owns `check_quality` (categorical gate), `compare_by_profile` (order by profile
  index, not raw resolution), and the among-allowed score (seeders/size).
- Pin `guessit` **exactly** (`==4.0.2`). The parse output drives the
  safety-critical cutoff, so an unsupervised upgrade must never shift
  classification; golden tests over known release names (including the
  prototype's leak cases) guard the mapping on every run.

**Unknown is rejected, not accepted.** `guessit` does not recognise every CAM/TS
variant (e.g. `HQCAM` returns no source). An unrecognised source maps to
`QualitySource.UNKNOWN`, which is `allowed = false` by default — the *safe*
direction. A small supplementary reject-keyword net layered on top only ever
*adds* rejections; it never promotes a release to acceptable.

## Consequences

**Positive**
- Maximises "borrow proven brains": `guessit` is a decade-plus-old, widely
  deployed parser (Bazarr et al.), so we inherit a battle-tested `source`
  taxonomy instead of hand-rolling regexes.
- Zero conflict with our owned quality model — `guessit` carries no ranking.
- The seam is honest: swapping or A/B-testing a second parser (RTN/PTT) later
  needs no domain change, only another `ParserPort` adapter.

**Negative / risks**
- `guessit` is not `py.typed` and pulls `rebulk` / `babelfish` /
  `python-dateutil`. Its untyped `MatchesDict` is confined to the adapter, which
  validates it into a typed `ParsedRelease`; the domain never sees the raw dict.
- We must maintain the `guessit source -> QualitySource` mapping table. It is the
  single most important correctness path, so it is golden-tested against the
  exact names the prototype leaked on.

## Alternatives considered

- **RTN (parse *and* rank)** — native Pydantic-v2 typed output, torrent-tuned.
  Rejected as the primary: its additive weighted ranking competes with our
  ordered profile + hard cutoff, and adopting it means deliberately discarding
  half the library. Strong runner-up as a *parse-only* second adapter behind the
  same port if native typing later outweighs `guessit`'s longevity.
- **parse-torrent-title (PTN)** — parser-only, lightweight, but thinner field
  coverage and effectively in maintenance mode (its modern successor is the
  PTT/parsett parser inside RTN). Rejected.
- **Port Radarr's parsing regexes wholesale** — re-derives the parser we set out
  to borrow, including Python `re`'s lack of variable-length lookbehind. We port
  Radarr's *quality taxonomy and weights* (data), not its parser (code).
