"""Issue #205 — lifecycle statuses must be typed enums in the OpenAPI schema.

Builds the exported schema the same way ``openapi_export`` does and asserts the
four wire ``status`` fields resolve (directly, or through a ``$ref``) to an
``enum`` component whose value set equals the canonical backend StrEnum. This
guards against both regressing back to a free ``string`` and silently
drifting the enum values out of sync with the canonical vocabulary (a rename
or addition to ``RequestStatus``/``DownloadState``/``DownloadScopeStatus``
without updating the schema/openapi.json fails here too, not just in CI's
``git diff --exit-code docs/api/openapi.json`` gate).
"""

from __future__ import annotations

from typing import Any

from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import DownloadScopeStatus, RequestStatus
from plex_manager.web.app import create_app


def _schema() -> dict[str, Any]:
    return create_app().openapi()


def _resolve_status_enum(schema: dict[str, Any], model_name: str) -> set[str]:
    """Return the ``enum`` value set for ``model_name``'s ``status`` property.

    Follows a ``$ref`` to the referenced component if present (FastAPI/Pydantic
    hoist a shared StrEnum into its own ``components.schemas`` entry rather than
    inlining it); falls back to an inline ``enum`` for robustness.
    """
    components = schema["components"]["schemas"]
    prop = components[model_name]["properties"]["status"]
    ref = prop.get("$ref")
    if ref is None:
        # Optional/defaulted fields may be wrapped as {"allOf": [{"$ref": ...}], ...}
        # or carry the $ref alongside a "default" key — handle both shapes.
        all_of = prop.get("allOf")
        if all_of:
            ref = all_of[0]["$ref"]
    assert ref is not None, f"{model_name}.status is not a typed $ref: {prop!r}"
    target_name = ref.rsplit("/", 1)[-1]
    target = components[target_name]
    assert target.get("type") == "string", f"{target_name} is not a string enum: {target!r}"
    enum_values = target.get("enum")
    assert enum_values is not None, f"{target_name} has no enum member list: {target!r}"
    return set(enum_values)


def test_request_response_status_is_request_status_enum() -> None:
    schema = _schema()
    assert _resolve_status_enum(schema, "RequestResponse") == {s.value for s in RequestStatus}


def test_season_status_status_is_request_status_enum() -> None:
    schema = _schema()
    assert _resolve_status_enum(schema, "SeasonStatus") == {s.value for s in RequestStatus}


def test_queue_item_status_is_download_state_enum() -> None:
    schema = _schema()
    assert _resolve_status_enum(schema, "QueueItem") == {s.value for s in DownloadState}


def test_queue_scope_status_is_download_scope_status_enum() -> None:
    schema = _schema()
    assert _resolve_status_enum(schema, "QueueScope") == {s.value for s in DownloadScopeStatus}


def test_no_lifecycle_status_field_is_a_bare_string() -> None:
    """A future regression (retyping back to ``str``) must fail this test, not
    just the ``make openapi`` git-diff gate — this asserts on the live schema
    object, independent of whether ``docs/api/openapi.json`` was regenerated."""
    schema = _schema()
    components = schema["components"]["schemas"]
    for model_name in ("RequestResponse", "SeasonStatus", "QueueItem", "QueueScope"):
        prop = components[model_name]["properties"]["status"]
        assert "$ref" in prop or "allOf" in prop, (
            f"{model_name}.status regressed to an untyped field: {prop!r}"
        )
