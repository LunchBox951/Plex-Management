"""Adapters — concrete implementations of the ``ports`` interfaces.

The outside world lives here: TMDB, Prowlarr, the qBittorrent download client,
Plex, the local filesystem, and the borrowed release parser. Adapters are the
only modules permitted to import third-party service SDKs. Implementations land
in the v1-planning session.
"""
