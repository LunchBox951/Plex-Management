"""GuessitParser — the :class:`ParserPort` implementation backed by guessit.

This is the ONLY module in the codebase that imports ``guessit``; the third-party
parser is confined here (ADR-0008). The adapter does no classification of its
own: it calls ``guessit`` to get a loosely-typed field mapping, then delegates
*all* source/resolution/modifier mapping to
:func:`plex_manager.domain.source_mapping.to_parsed_release`, which owns the
safety-critical CAM/TS reject logic. Keeping the mapping in the pure domain means
it is golden-tested without importing guessit.

Typing note: ``guessit.guessit`` is annotated upstream as returning
``dict[str, Any]``. We widen that to ``Mapping[str, object]`` (the domain's
boundary type) at the call site so the ``Any`` values cannot silently leak
``Unknown`` types past this module. No ``# pyright: ignore`` is required.
"""

from __future__ import annotations

from collections.abc import Mapping

from guessit import guessit

from plex_manager.domain.release import ParsedRelease
from plex_manager.domain.source_mapping import to_parsed_release

__all__ = ["GuessitParser"]


class GuessitParser:
    """Parse raw release names into :class:`ParsedRelease` via guessit.

    Implements :class:`plex_manager.ports.parser.ParserPort`. Parsing is
    synchronous (guessit is CPU-bound, no I/O) and never raises: a name guessit
    cannot classify yields a :class:`ParsedRelease` with ``UNKNOWN`` source,
    which the default quality profile rejects.
    """

    def parse(self, release_name: str) -> ParsedRelease:
        """Parse ``release_name`` into a typed :class:`ParsedRelease`.

        The untyped guessit result is treated as ``Mapping[str, object]`` at the
        boundary and handed to the domain mapper; this adapter intentionally
        performs no classification itself.
        """
        fields: Mapping[str, object] = guessit(release_name)
        return to_parsed_release(fields, raw_title=release_name)
