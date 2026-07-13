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


def _references_error_detail(response: dict[str, Any]) -> bool:
    """Whether a response object's schema references ``ErrorDetail``, directly or
    inside an ``anyOf`` (a status code with more than one producer shape)."""
    content = response.get("content", {})
    body_schema = content.get("application/json", {}).get("schema", {})
    if body_schema.get("$ref") == "#/components/schemas/ErrorDetail":
        return True
    any_of = body_schema.get("anyOf", [])
    return any(entry.get("$ref") == "#/components/schemas/ErrorDetail" for entry in any_of)


def test_report_issue_declares_404_409_422() -> None:
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/requests/{request_id}/report-issue", "post")
    assert "404" in responses, responses.keys()
    assert "409" in responses, responses.keys()
    assert "422" in responses, responses.keys()
    assert _references_error_detail(responses["404"])
    assert _references_error_detail(responses["409"])
    assert _references_error_detail(responses["422"])


def test_cancel_request_declares_404_409() -> None:
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/requests/{request_id}/cancel", "post")
    assert "404" in responses, responses.keys()
    assert "409" in responses, responses.keys()
    assert _references_error_detail(responses["404"])
    assert _references_error_detail(responses["409"])


def test_relocate_declares_404_409() -> None:
    schema = _schema()
    responses = _responses_for(schema, "/api/v1/queue/{download_id}/relocate", "post")
    assert "404" in responses, responses.keys()
    assert "409" in responses, responses.keys()
    assert _references_error_detail(responses["404"])
    assert _references_error_detail(responses["409"])
