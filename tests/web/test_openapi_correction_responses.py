"""Issue #112 — correction endpoints must declare their manually-raised statuses.

``report-issue``/``cancel``/``relocate`` (ADR-0014 correction verbs) raise 404/409/
422 ``HTTPException``s (plus, for report-issue, an ``AppError`` 409 and, for
cancel, a ``ServiceNotConfiguredError`` 409) that FastAPI cannot infer from the
return type alone. Undeclared status codes are silently missing from
``docs/api/openapi.json`` and the generated TS client, so a caller has no typed
way to branch on them. This asserts the exported schema's ``responses`` map for
each correction path actually lists every status code the handler raises,
referencing the project's ``ErrorDetail`` model (directly, or via an ``anyOf``
that includes it for a status code with more than one producer shape).

Issue #291 extends this to the ``service_not_configured`` 409 specifically:
report-issue (Plex/qBittorrent/Prowlarr, all non-optional deps) and the queue
mutation endpoints -- grab, import, mark-failed, relocate, all sharing
``queue.py``'s ``_QUEUE_ERROR_RESPONSES`` -- can each raise the app-wide
``ServiceNotConfiguredError`` 409, whose body carries a ``service`` field that
the bare ``ErrorDetail`` model has no field for. Those tests assert the
generated schema's ``responses`` map for the affected status code also
references ``ServiceNotConfiguredErrorDetail`` (directly or via ``anyOf``), so
the ``service`` field is not lost to the generic shape in the TS client.
"""

from __future__ import annotations

from typing import Any

from plex_manager.web.app import create_app


def _schema() -> dict[str, Any]:
    return create_app().openapi()


def _responses_for(schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    operation = schema["paths"][path][method]
    responses: dict[str, Any] = operation["responses"]
    return responses


def _references_schema(response: dict[str, Any], ref_name: str) -> bool:
    """Whether a response object's schema references ``ref_name``, directly or
    inside an ``anyOf`` (a status code with more than one producer shape)."""
    content = response.get("content", {})
    body_schema = content.get("application/json", {}).get("schema", {})
    ref = f"#/components/schemas/{ref_name}"
    if body_schema.get("$ref") == ref:
        return True
    any_of = body_schema.get("anyOf", [])
    return any(entry.get("$ref") == ref for entry in any_of)


def _references_error_detail(response: dict[str, Any]) -> bool:
    return _references_schema(response, "ErrorDetail")


def _references_service_not_configured(response: dict[str, Any]) -> bool:
    return _references_schema(response, "ServiceNotConfiguredErrorDetail")


def test_report_issue_declares_404_409_422() -> None:
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/requests/{request_id}/report-issue", "post")
    assert "404" in responses, responses.keys()
    assert "409" in responses, responses.keys()
    assert "422" in responses, responses.keys()
    assert _references_error_detail(responses["404"])
    assert _references_error_detail(responses["409"])
    assert _references_error_detail(responses["422"])


def test_report_issue_409_declares_service_not_configured() -> None:
    """Plex/qBittorrent/Prowlarr are all non-optional deps here (issue #291) --
    an install missing any of them 409s the same ``service_not_configured`` shape
    cancel's does, so it must be typed here too."""
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/requests/{request_id}/report-issue", "post")
    assert _references_service_not_configured(responses["409"])


def test_cancel_request_declares_404_409() -> None:
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/requests/{request_id}/cancel", "post")
    assert "404" in responses, responses.keys()
    assert "409" in responses, responses.keys()
    assert _references_error_detail(responses["404"])
    assert _references_error_detail(responses["409"])


def test_cancel_request_409_declares_service_not_configured() -> None:
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/requests/{request_id}/cancel", "post")
    assert _references_service_not_configured(responses["409"])


def test_relocate_declares_404_409() -> None:
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/queue/{download_id}/relocate", "post")
    assert "404" in responses, responses.keys()
    assert "409" in responses, responses.keys()
    assert _references_error_detail(responses["404"])
    assert _references_error_detail(responses["409"])


def test_relocate_409_declares_service_not_configured() -> None:
    """qBittorrent is a non-optional dep here (issue #291)."""
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/queue/{download_id}/relocate", "post")
    assert _references_service_not_configured(responses["409"])


def test_import_409_declares_service_not_configured() -> None:
    """Incidental coverage (issue #291): import shares ``_QUEUE_ERROR_RESPONSES``
    with relocate, and Plex/qBittorrent are non-optional deps here too."""
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/queue/{download_id}/import", "post")
    assert "409" in responses, responses.keys()
    assert _references_service_not_configured(responses["409"])


def test_mark_failed_409_declares_service_not_configured() -> None:
    """Incidental coverage (issue #291): mark-failed shares
    ``_QUEUE_ERROR_RESPONSES`` with relocate, and itself raises
    ``ServiceNotConfiguredError`` directly when ``remove_torrent=true`` and
    qBittorrent is unconfigured."""
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/queue/{download_id}/mark-failed", "post")
    assert "409" in responses, responses.keys()
    assert _references_service_not_configured(responses["409"])


def test_grab_409_declares_service_not_configured() -> None:
    """Incidental coverage (issue #291): grab's ``_GRAB_ERROR_RESPONSES`` spreads
    ``_QUEUE_ERROR_RESPONSES``, and qBittorrent/Prowlarr are non-optional deps
    here too."""
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/queue/grab", "post")
    assert "409" in responses, responses.keys()
    assert _references_service_not_configured(responses["409"])
