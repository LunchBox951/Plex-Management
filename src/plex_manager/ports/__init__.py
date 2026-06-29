"""Ports — the typed interfaces the domain core depends on.

Each port is satisfied by an adapter in the ``adapters`` package. Planned ports
(defined in the v1-planning session): ``MetadataPort`` (TMDB), ``IndexerPort``
(Prowlarr), ``DownloadClientPort`` (qBittorrent — see ADR-0006), ``LibraryPort``
(Plex), ``FileSystemPort``, and ``ParserPort`` (the borrowed release parser).
"""
