"""Run the privileged updater sidecar."""

from __future__ import annotations

import asyncio
import logging

from plex_manager.updater.config import UpdaterConfig, UpdaterConfigError
from plex_manager.updater.coordinator import CoordinatorClient
from plex_manager.updater.engine import DockerEngine
from plex_manager.updater.runner import UpdaterRunner
from plex_manager.updater.state import StateError, StateStore


async def _run() -> None:
    config = UpdaterConfig.from_env()
    token = config.read_secret()
    engine = DockerEngine(config.docker_socket)
    coordinator = CoordinatorClient(
        config.coordinator_url,
        token,
        timeout=config.request_timeout_seconds,
    )
    state = StateStore(config.state_file)
    try:
        with state:
            runner = UpdaterRunner(config, engine, coordinator, state)
            await runner.run_forever()
    finally:
        await coordinator.close()
        await engine.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_run())
    except (UpdaterConfigError, StateError) as exc:
        # Fixed exception messages only; secret contents and Docker bodies are
        # never interpolated into bootstrap failures.
        logging.getLogger(__name__).error("container updater cannot start: %s", str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
