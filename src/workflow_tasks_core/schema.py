"""
JSON Schema helpers for a Fractal task's ``args_schema_*``, as written by
fractal-task-tools / fractal-tasks-core manifests.

No Qt here: the plugin builds its task forms (``_schema_form.py``) on top of
these helpers.
"""

from __future__ import annotations

from typing import Optional

# Arguments injected by the launcher (Fractal "reserved" arguments)
RESERVED_ARGS = {"zarr_url", "zarr_urls", "zarr_dir", "init_args"}


class SchemaError(ValueError):
    """Invalid value in a field; ``.path`` names the field."""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path


# --------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------


def with_defs(schema: Optional[dict]) -> dict:
    """A task's args schema as a standalone object schema."""
    schema = dict(schema or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def resolve(schema: dict, defs: dict) -> dict:
    """Follow ``$ref`` (and single-item ``allOf``), keeping local keys such
    as title/description/default from the referencing schema."""
    schema = dict(schema)
    seen = set()
    while True:
        if "$ref" in schema:
            ref = schema.pop("$ref")
            if ref in seen:
                break
            seen.add(ref)
            target = defs.get(ref.split("/")[-1], {})
            schema = {**target, **schema}
        elif "allOf" in schema and len(schema["allOf"]) == 1:
            sub = schema.pop("allOf")[0]
            schema = {**sub, **schema}
        else:
            break
    return schema


def schema_variants(schema: dict) -> list[dict]:
    """The alternatives of a ``oneOf`` / ``anyOf`` schema, [] if none."""
    return schema.get("oneOf") or schema.get("anyOf") or []


def is_null(schema: dict) -> bool:
    return schema.get("type") == "null"


def schema_type(schema: dict) -> Optional[str]:
    """The JSON type of a schema; ``[X, "null"]`` counts as X."""
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        return non_null[0] if len(non_null) == 1 else None
    return t
