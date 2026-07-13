from __future__ import annotations

import logging

import httpx
import pytest

from plex_manager.config import Settings, get_settings
from plex_manager.services import path_visibility
from plex_manager.web.app import (
    _BARE_METAL_HOST_PORT_NOTE,  # pyright: ignore[reportPrivateUsage]
    _UNCONFIRMED_HOST_PORT_NOTE,  # pyright: ignore[reportPrivateUsage]
    _emit_setup_ready_hint,  # pyright: ignore[reportPrivateUsage]
    _running_under_documented_compose,  # pyright: ignore[reportPrivateUsage]
    _setup_ready_url,  # pyright: ignore[reportPrivateUsage]
    _warn_if_multi_process,  # pyright: ignore[reportPrivateUsage]
    create_upstream_http_client,
)


def _always_a_live_mount(_path: str) -> bool:
    """A ``path_visibility.is_live_mount`` stand-in simulating the documented
    compose topology's required bind mounts always being live."""
    return True


def _never_a_live_mount(_path: str) -> bool:
    """A ``path_visibility.is_live_mount`` stand-in simulating a bare-metal
    install with neither of the compose-required bind mounts present."""
    return False


def _only_media_is_a_live_mount(path: str) -> bool:
    """A ``path_visibility.is_live_mount`` stand-in simulating a HALF-mounted
    bare-metal box: a real disk mounted directly at ``/media`` (its own,
    unrelated mount point -- nothing to do with the documented Compose
    topology) but no ``/downloads`` mount, since ``docker-compose.yml``
    requires BOTH."""
    return path == "/media"


async def test_upstream_http_client_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")

    client = create_upstream_http_client()
    try:
        assert client._trust_env is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await client.aclose()


async def test_upstream_http_client_rejects_response_cookies() -> None:
    client = create_upstream_http_client()
    try:
        request = httpx.Request("GET", "http://service.local:8080/login")
        response = httpx.Response(
            200,
            headers={"Set-Cookie": "SID=service-secret; Path=/"},
            request=request,
        )
        client.cookies.extract_cookies(response)
        assert list(client.cookies.jar) == []
    finally:
        await client.aclose()


class TestRunningUnderDocumentedCompose:
    """``_running_under_documented_compose`` gates the compose-only
    ``host_port``/``host_bind`` settings on BOTH documented bind mounts being
    live, not just one (P2 follow-up to issue #294)."""

    def test_true_when_both_documented_mounts_are_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(path_visibility, "is_live_mount", _always_a_live_mount)
        assert _running_under_documented_compose() is True

    def test_false_when_neither_documented_mount_is_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(path_visibility, "is_live_mount", _never_a_live_mount)
        assert _running_under_documented_compose() is False

    def test_false_when_only_one_documented_mount_is_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare-metal box can easily have a real, unrelated disk mounted
        directly at ``/media`` while having no ``/downloads`` mount at all --
        ``docker-compose.yml`` requires BOTH, so this must not be classified
        as the documented Compose topology (an ``any()`` gate would wrongly
        say otherwise)."""
        monkeypatch.setattr(path_visibility, "is_live_mount", _only_media_is_a_live_mount)
        assert _running_under_documented_compose() is False


class TestSetupReadyUrl:
    """The startup setup-URL hint (issue #65) -- see Codex's follow-up findings:
    the printed link must use the externally-reachable HOST port, not always the
    in-container one, and must never carry the bootstrap token as a query string
    (which uvicorn's default access log would otherwise record verbatim)."""

    def test_uses_host_port_when_running_under_the_documented_compose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate the documented compose topology's required bind mounts (see
        # `_running_under_documented_compose`) -- a real docker-compose install
        # always has BOTH of these live; a bare-metal test process never does.
        monkeypatch.setattr(path_visibility, "is_live_mount", _always_a_live_mount)
        settings = Settings(host="0.0.0.0", port=8000, host_port=9443)  # noqa: S104
        url = _setup_ready_url(settings)
        assert url.startswith("http://localhost:9443/setup")
        assert _UNCONFIRMED_HOST_PORT_NOTE not in url
        assert _BARE_METAL_HOST_PORT_NOTE not in url

    def test_falls_back_to_the_real_listener_when_only_one_mount_is_documented(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2 follow-up to issue #294, finding 3/4: a bare-metal box with a
        real (but unrelated) ``/media`` mount plus copied ``.env`` defaults for
        ``PLEX_MANAGER_HOST_PORT``/``PLEX_MANAGER_HOST_BIND`` must NOT be
        misclassified as the documented Compose topology just because ONE of
        the two required mounts happens to be live -- it must fall back to
        the actual in-process listener (``settings.port``/``settings.host``),
        the same as the fully-unmounted bare-metal case."""
        monkeypatch.setattr(path_visibility, "is_live_mount", _only_media_is_a_live_mount)
        settings = Settings(
            host="127.0.0.1",
            port=9000,
            host_port=8000,
            host_bind="192.168.1.50",
        )
        url = _setup_ready_url(settings)
        assert url.startswith("http://127.0.0.1:9000/setup")
        assert _BARE_METAL_HOST_PORT_NOTE in url
        assert _UNCONFIRMED_HOST_PORT_NOTE not in url

    def test_falls_back_to_the_in_container_port_and_says_so_when_unknown(self) -> None:
        settings = Settings(host="0.0.0.0", port=8000, host_port=None)  # noqa: S104
        url = _setup_ready_url(settings)
        assert url.startswith("http://localhost:8000/setup")
        assert _UNCONFIRMED_HOST_PORT_NOTE in url

    def test_ignores_a_compose_only_host_port_default_on_bare_metal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #294, finding 3: a bare-metal install that copies
        ``.env.example`` verbatim inherits its compose-only
        ``PLEX_MANAGER_HOST_PORT=8000`` default even though no port mapping was
        ever applied. Without the compose-topology gate, that guessed port
        would print unchallenged; with it, the app falls back to the
        in-container port and says so honestly."""
        monkeypatch.setattr(path_visibility, "is_live_mount", _never_a_live_mount)
        settings = Settings(host="0.0.0.0", port=9000, host_port=8000)  # noqa: S104
        url = _setup_ready_url(settings)
        assert url.startswith("http://localhost:9000/setup")
        assert _BARE_METAL_HOST_PORT_NOTE in url
        assert _UNCONFIRMED_HOST_PORT_NOTE not in url

    def test_substitutes_localhost_for_an_undialable_bind_host(self) -> None:
        settings = Settings(host="::", port=8000, host_port=8000)
        url = _setup_ready_url(settings)
        assert url.startswith("http://localhost:8000/setup")

    def test_carries_the_token_in_a_fragment_never_a_query_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
        get_settings.cache_clear()
        settings = get_settings()

        url = _setup_ready_url(settings)

        assert "#setup_token=boot-token" in url
        assert "?setup_token=" not in url

    def test_omits_the_token_entirely_when_unset(self) -> None:
        settings = Settings(host="127.0.0.1", port=8000, host_port=8000)
        url = _setup_ready_url(settings)
        assert "setup_token" not in url

    def test_keeps_the_setup_token_fragment_attached_to_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2 follow-up to issue #294, finding 1: when a setup token AND a
        non-empty port note are both present, the clickable URL (fragment
        included) must be one contiguous, space-free line, with the note
        confined to its own separate line.

        A prior revision spliced the note's prose directly between the path
        and the ``#setup_token=`` fragment on the SAME line (note still
        "before the fragment", just not on its own line). Because that prose
        contains spaces, a terminal linkifier recognizes only the bare
        ``.../setup`` prefix (stopping at the first space) as the clickable
        link -- silently dropping the fragment -- while copying the WHOLE
        printed line verbatim produces a request for a literal
        ``/setup <prose...>`` path that the React ``/setup`` route does not
        match, so the wizard never consumes the token either way.
        """
        monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
        get_settings.cache_clear()
        settings = get_settings()

        url = _setup_ready_url(settings)

        # No PLEX_MANAGER_HOST_PORT is configured in this test environment, so
        # `_setup_ready_url` also attaches `_UNCONFIRMED_HOST_PORT_NOTE` --
        # exercising exactly the token+note combination the finding was about.
        assert _UNCONFIRMED_HOST_PORT_NOTE in url

        url_line = url.splitlines()[0]
        assert " " not in url_line
        assert url_line.endswith("#setup_token=boot-token")

    def test_uses_the_published_host_bind_over_the_in_process_listen_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #294, finding 4: a Compose install deliberately published
        under a LAN IP must get that IP in the printed link, not an
        unconditional ``localhost`` that never resolves off the container
        host. ``host_port`` is deliberately distinct from ``port`` here so
        this only passes via the compose branch honoring both published
        values -- not by coincidence with the bare-metal fallback (which
        would print ``port``, not ``host_port``)."""
        monkeypatch.setattr(path_visibility, "is_live_mount", _always_a_live_mount)
        settings = Settings(
            host="0.0.0.0",  # noqa: S104
            port=8000,
            host_port=9443,
            host_bind="192.168.1.50",
        )
        url = _setup_ready_url(settings)
        assert url.startswith("http://192.168.1.50:9443/setup")
        assert _BARE_METAL_HOST_PORT_NOTE not in url

    def test_substitutes_localhost_when_the_published_host_bind_is_also_undialable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(path_visibility, "is_live_mount", _always_a_live_mount)
        settings = Settings(
            host="0.0.0.0",  # noqa: S104
            port=8000,
            host_port=9443,
            host_bind="0.0.0.0",  # noqa: S104
        )
        url = _setup_ready_url(settings)
        assert url.startswith("http://localhost:9443/setup")

    def test_falls_back_to_settings_host_when_host_bind_is_unset(self) -> None:
        settings = Settings(host="127.0.0.1", port=8000, host_port=8000, host_bind=None)
        url = _setup_ready_url(settings)
        assert url.startswith("http://127.0.0.1:8000/setup")

    def test_ignores_a_compose_only_host_bind_default_on_bare_metal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare-metal install that copies ``.env.example`` verbatim inherits
        its compose-only ``PLEX_MANAGER_HOST_BIND=127.0.0.1`` default -- and an
        operator who then widens it for what they believe is a Compose LAN
        publish would otherwise get that value trusted unconditionally even
        though the bare-metal process actually listens on ``settings.host``.
        Without the compose-topology gate this prints an undialable link
        exactly like the ungated ``host_port`` bug finding 3 already closes;
        with it, ``host_bind`` is ignored and the in-process host is used."""
        monkeypatch.setattr(path_visibility, "is_live_mount", _never_a_live_mount)
        settings = Settings(
            host="127.0.0.1",
            port=8000,
            host_port=8000,
            host_bind="192.168.1.50",
        )
        url = _setup_ready_url(settings)
        assert url.startswith("http://127.0.0.1:8000/setup")


def test_emit_setup_ready_hint_writes_the_real_unredacted_url_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole point of issue #65 is a link an operator can follow straight
    from ``docker logs``. The production entry point runs uvicorn with its own
    default logging config, which attaches nothing to the ROOT logger this
    module's ``_logger`` propagates to -- and the app's own root handler
    (``LogCaptureHandler``) redacts ``token=...`` shapes as defense in depth
    before persisting them. Neither hazard may swallow this specific line, so it
    must reach stderr as a direct, unredacted write.
    """
    url = "http://localhost:8000/setup#setup_token=boot-token"

    _emit_setup_ready_hint(url)

    captured = capsys.readouterr()
    assert f"Setup: {url}" in captured.err


def test_emit_setup_ready_hint_survives_a_root_logger_with_no_stderr_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reproduces the exact production shape: the root logger has SOME handler
    attached (as ``configure_logging`` leaves it, via ``LogCaptureHandler``) but
    nothing that writes to stderr. A plain ``_logger.info(...)`` call has no
    path to the console in this shape; the hint must not depend on it.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    for handler in saved_handlers:
        root.removeHandler(handler)
    root.addHandler(logging.NullHandler())
    try:
        _emit_setup_ready_hint("http://localhost:8000/setup#setup_token=boot-token")
    finally:
        root.removeHandler(root.handlers[-1])
        for handler in saved_handlers:
            root.addHandler(handler)

    captured = capsys.readouterr()
    assert "boot-token" in captured.err


def test_emit_setup_ready_hint_also_records_a_copy_for_the_in_app_log_viewer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="plex_manager.web.app"):
        _emit_setup_ready_hint("http://localhost:8000/setup#setup_token=boot-token")

    assert any("Setup: http://localhost:8000/setup" in r.message for r in caplog.records)


def _clear_multiworker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "WORKERS", "GUNICORN_CMD_ARGS"):
        monkeypatch.delenv(var, raising=False)


def test_warn_if_multi_process_is_silent_by_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_multiworker_env(monkeypatch)
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            _warn_if_multi_process()
        assert caplog.text == ""
    finally:
        get_settings.cache_clear()


def test_warn_if_multi_process_is_silent_when_set_to_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_multiworker_env(monkeypatch)
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            _warn_if_multi_process()
        assert caplog.text == ""
    finally:
        get_settings.cache_clear()


def test_warn_if_multi_process_warns_loudly_above_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Issue #240: this app's in-process removal-physics/settings-rotation
    # guards silently reopen their races across more than one worker process --
    # make that violated assumption LOUD at startup instead.
    _clear_multiworker_env(monkeypatch)
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            _warn_if_multi_process()
        assert "WEB_CONCURRENCY" in caplog.text
        assert "single" in caplog.text.lower()
    finally:
        get_settings.cache_clear()


def test_warn_if_multi_process_warns_on_signals_other_than_web_concurrency(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Codex review (PR #281): the in-process registries are broken just as much
    # by a gunicorn/UVICORN_WORKERS/WORKERS-driven scale-out as by
    # WEB_CONCURRENCY -- this warning must fire on ANY of the signals
    # ``web.events.detect_multiworker_signals`` detects, not just that one.
    _clear_multiworker_env(monkeypatch)
    monkeypatch.setenv("UVICORN_WORKERS", "4")
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            _warn_if_multi_process()
        assert "UVICORN_WORKERS" in caplog.text
        assert "single" in caplog.text.lower()
    finally:
        get_settings.cache_clear()
