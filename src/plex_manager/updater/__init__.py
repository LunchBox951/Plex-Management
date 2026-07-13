"""Privileged container updater sidecar.

The package is shipped in the ordinary Plex Manager image, but only the
``updater`` Compose service executes it and receives the Docker control socket.
It deliberately has no dependency on the web application or database models:
policy and maintenance coordination remain behind the authenticated internal
HTTP API.
"""

from plex_manager.updater.config import UpdaterConfig
from plex_manager.updater.runner import UpdaterRunner

__all__ = ["UpdaterConfig", "UpdaterRunner"]
