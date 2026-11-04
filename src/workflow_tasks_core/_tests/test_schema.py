"""JSON Schema helpers for task argument schemas."""

from workflow_tasks_core.schema import (
    SchemaError,
    is_null,
    resolve,
    schema_type,
    schema_variants,
    with_defs,
)


def test_resolve_follows_ref_and_keeps_local_keys():
    defs = {"Channel": {"type": "object", "title": "Channel"}}
    schema = {"$ref": "#/$defs/Channel", "title": "Input channel"}
    assert resolve(schema, defs) == {"type": "object",
                                     "title": "Input channel"}
    assert resolve({"allOf": [{"$ref": "#/$defs/Channel"}]}, defs) == {
        "type": "object", "title": "Channel"}


def test_resolve_stops_on_cycles():
    defs = {"A": {"$ref": "#/$defs/A"}}
    assert resolve({"$ref": "#/$defs/A"}, defs) == {}


def test_schema_type():
    assert schema_type({"type": "integer"}) == "integer"
    assert schema_type({"type": ["string", "null"]}) == "string"
    assert schema_type({"type": ["string", "integer"]}) is None
    assert schema_type({}) is None


def test_variants_and_null():
    options = [{"type": "string"}, {"type": "null"}]
    assert schema_variants({"anyOf": options}) == options
    assert schema_variants({"oneOf": options}) == options
    assert schema_variants({"type": "string"}) == []
    assert [is_null(o) for o in options] == [False, True]


def test_with_defs():
    assert with_defs(None) == {"type": "object", "properties": {}}
    schema = {"properties": {"a": {}}, "$defs": {"X": {}}}
    assert with_defs(schema) == {**schema, "type": "object"}
    assert "type" not in schema  # the task's schema is left as is


def test_schema_error_names_the_field():
    err = SchemaError("channel.identifier", "required")
    assert str(err) == "channel.identifier: required"
    assert err.path == "channel.identifier"
    assert isinstance(err, ValueError)
