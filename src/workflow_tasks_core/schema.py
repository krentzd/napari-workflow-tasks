"""
JSON Schema helpers for a Fractal task's ``args_schema_*``, as written by
fractal-task-tools / fractal-tasks-core manifests.

No Qt here: the plugin builds its task forms (``_schema_form.py``) on top of
these helpers.
"""

from __future__ import annotations

import difflib
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


# --------------------------------------------------------------------------
# Checking arguments (headless API: fail fast, before starting a task)
# --------------------------------------------------------------------------

# JSON type -> (Python types, name in messages, name in type_name())
_TYPES = {
    "string": ((str,), "a string", "str"),
    "integer": ((int,), "an integer", "int"),
    "number": ((int, float), "a number", "float"),
    "boolean": ((bool,), "a boolean", "bool"),
    "array": ((list, tuple), "a list", "list"),
    "object": ((dict,), "a dict", "dict"),
    "null": ((type(None),), "None", "None"),
}


def _is_type(value, json_type: str) -> bool:
    types = _TYPES.get(json_type, ((object,),))[0]
    if isinstance(value, bool) and json_type in ("integer", "number"):
        return False
    return isinstance(value, types)


def _join(path: str, key) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _choices(values) -> str:
    return ", ".join(repr(v) for v in values)


def pick_variant(value, schema: dict, variants: list, defs: dict):
    """The variant a discriminator (or a matching ``const``) selects; None
    if the value names no variant."""
    if not isinstance(value, dict):
        return None
    disc = schema.get("discriminator") or {}
    prop = disc.get("propertyName")
    if prop and prop in value:
        ref = (disc.get("mapping") or {}).get(value[prop])
        if ref:
            return resolve({"$ref": ref}, defs)
    for variant in variants:
        v = resolve(variant, defs)
        for key, sub in (v.get("properties") or {}).items():
            if "const" in sub and value.get(key) == sub["const"]:
                return v
    return None


def _check(value, schema: dict, defs: dict, path: str, errors: list) -> None:
    schema = resolve(schema, defs)
    variants = schema_variants(schema)
    if variants:
        nullable = any(is_null(resolve(v, defs)) for v in variants)
        if value is None:
            if not nullable:
                errors.append(SchemaError(path, "must not be None"))
            return
        picked = pick_variant(value, schema, variants, defs)
        if picked is not None:
            _check(value, picked, defs, path, errors)
            return
        others = [resolve(v, defs) for v in variants]
        others = [v for v in others if not is_null(v)]
        # One alternative besides None: report its errors directly
        if len(others) == 1:
            sub = []
            _check(value, others[0], defs, path, sub)
            for e in sub:
                msg = str(e)[len(path) + 2:]
                if nullable and e.path == path and msg.startswith("expected "):
                    e = SchemaError(path, msg.replace(", got", " or None, got", 1))
                errors.append(e)
            return
        for variant in others:
            sub = []
            _check(value, variant, defs, path, sub)
            if not sub:
                return
        # Discriminated union with an unknown discriminator value
        disc = (schema.get("discriminator") or {}).get("propertyName")
        if disc and isinstance(value, dict):
            consts = [
                (v.get("properties") or {}).get(disc, {}).get("const")
                for v in others
            ]
            errors.append(SchemaError(
                _join(path, disc), f"must be one of {_choices(consts)}"))
            return
        errors.append(SchemaError(
            path, f"does not match any of: {type_name(schema, defs)}"))
        return

    if "const" in schema:
        if value != schema["const"]:
            errors.append(SchemaError(path, f"must be {schema['const']!r}"))
        return
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(SchemaError(
                path, f"must be one of {_choices(schema['enum'])}"))
        return

    json_type = schema.get("type")
    types = json_type if isinstance(json_type, list) else (
        [json_type] if json_type else [])
    if types and not any(_is_type(value, t) for t in types):
        expected = " or ".join(_TYPES.get(t, (None, t))[1] for t in types)
        errors.append(SchemaError(
            path, f"expected {expected}, got {type(value).__name__}"))
        return

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, op, ok in (
            ("minimum", ">=", lambda v, m: v >= m),
            ("maximum", "<=", lambda v, m: v <= m),
            ("exclusiveMinimum", ">", lambda v, m: v > m),
            ("exclusiveMaximum", "<", lambda v, m: v < m),
        ):
            if key in schema and not ok(value, schema[key]):
                errors.append(SchemaError(path, f"must be {op} {schema[key]}"))
    elif isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(SchemaError(
                path, f"at least {schema['minItems']} item(s) expected"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(SchemaError(
                path, f"at most {schema['maxItems']} item(s) expected"))
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                _check(item, schema["items"], defs, _join(path, i), errors)
    elif isinstance(value, dict):
        _check_object(value, schema, defs, path, errors, reserved=())


def _check_object(value: dict, schema: dict, defs: dict, path: str,
                  errors: list, reserved) -> None:
    props = schema.get("properties") or {}
    extra = schema.get("additionalProperties")
    for key, item in value.items():
        where = _join(path, key)
        if key in reserved:
            errors.append(SchemaError(where, "set by run(), not as an argument"))
        elif key in props:
            _check(item, props[key], defs, where, errors)
        elif isinstance(extra, dict):
            _check(item, extra, defs, where, errors)
        elif props and extra is not True:
            close = difflib.get_close_matches(
                key, [p for p in props if p not in reserved], n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            errors.append(SchemaError(where, f"unknown argument{hint}"))
    for key in schema.get("required") or []:
        if key not in value and key not in reserved:
            errors.append(SchemaError(_join(path, key), "required"))


def validate(args: dict, schema: Optional[dict],
             reserved=tuple(RESERVED_ARGS)) -> list[SchemaError]:
    """Problems with a task's arguments ``args`` (all of them, [] if none).

    Checks argument names (unknown ones, with the closest valid name),
    required arguments, types, ``enum``/``const`` values, number bounds and
    list lengths, also inside nested models and their variants. Arguments
    in ``reserved`` are filled in by the runner: they may not be given and
    are not required. The task itself still validates its arguments; this
    only catches mistakes before it starts.
    """
    schema = with_defs(schema)
    defs = schema.get("$defs") or {}
    errors: list[SchemaError] = []
    _check_object(dict(args), schema, defs, "", errors, reserved=set(reserved))
    return errors


def type_name(schema: dict, defs: dict) -> str:
    """Short Python-like name of a schema's type (``int | None``,
    ``list[str]``, a model's title, ``'a' | 'b'`` for an enum)."""
    ref = schema.get("$ref")
    resolved = resolve(schema, defs)
    variants = schema_variants(resolved)
    if variants:
        return " | ".join(type_name(v, defs) for v in variants)
    if ref:
        return defs.get(ref.split("/")[-1], {}).get("title") or ref.split("/")[-1]
    if "const" in resolved:
        return repr(resolved["const"])
    if "enum" in resolved:
        return " | ".join(repr(v) for v in resolved["enum"])
    json_type = resolved.get("type")
    if isinstance(json_type, list):
        return " | ".join(_TYPES.get(t, (None, None, t))[2] for t in json_type)
    if json_type == "array" and isinstance(resolved.get("items"), dict):
        return f"list[{type_name(resolved['items'], defs)}]"
    if json_type == "object" and resolved.get("title") and resolved.get("properties"):
        return resolved["title"]
    return _TYPES.get(json_type, (None, None, json_type or "any"))[2]
