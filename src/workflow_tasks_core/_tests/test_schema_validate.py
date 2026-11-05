"""Checking task arguments against a task's args schema before a run."""

import pytest

from workflow_tasks_core.schema import SchemaError, type_name, validate

DEFS = {
    "Channels": {
        "type": "object",
        "title": "Channels",
        "properties": {
            "mode": {"type": "string", "enum": ["label", "index"],
                     "default": "label"},
            "identifiers": {"type": "array", "items": {"type": "string"},
                            "minItems": 1, "maxItems": 3},
        },
        "required": ["identifiers"],
    },
    "Create": {
        "type": "object", "title": "Create",
        "properties": {
            "mode": {"const": "Create", "type": "string", "default": "Create"},
            "table_name": {"type": "string", "default": "{label_name}_ROI"},
        },
    },
    "Skip": {
        "type": "object", "title": "Skip",
        "properties": {"mode": {"const": "Skip", "type": "string",
                                "default": "Skip"}},
    },
}
SCHEMA = {
    "type": "object",
    "properties": {
        "zarr_url": {"type": "string"},
        "channels": {"$ref": "#/$defs/Channels"},
        "label_name": {"type": "string", "default": "x"},
        "level": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
                  "default": None},
        "diameter": {"type": "number"},
        "overwrite": {"type": "boolean", "default": True},
        "masking": {
            "oneOf": [{"$ref": "#/$defs/Create"}, {"$ref": "#/$defs/Skip"}],
            "discriminator": {"propertyName": "mode",
                              "mapping": {"Create": "#/$defs/Create",
                                          "Skip": "#/$defs/Skip"}},
            "default": {"mode": "Skip"},
        },
    },
    "required": ["zarr_url", "channels"],
    "$defs": DEFS,
}


def _errors(args, **kw):
    return [str(e) for e in validate(args, SCHEMA, **kw)]


def test_valid_arguments():
    assert _errors({"channels": {"identifiers": ["DAPI"]}, "level": 2,
                    "diameter": 30, "masking": {"mode": "Create"}}) == []


def test_reserved_arguments_are_not_required():
    # zarr_url is filled in by the runner
    assert _errors({"channels": {"identifiers": ["DAPI"]}}) == []


def test_missing_required():
    assert _errors({}) == ["channels: required"]


def test_unknown_argument_suggests_close_name():
    errors = _errors({"channels": {"identifiers": ["DAPI"]}, "labelname": "a"})
    assert errors == ["labelname: unknown argument (did you mean 'label_name'?)"]


def test_reserved_argument_given():
    errors = _errors({"channels": {"identifiers": ["DAPI"]}, "zarr_url": "/a"})
    assert errors == ["zarr_url: set by run(), not as an argument"]


def test_wrong_types():
    errors = _errors({"channels": {"identifiers": "DAPI"}, "overwrite": 1,
                      "diameter": "big", "label_name": 3})
    assert sorted(errors) == [
        "channels.identifiers: expected a list, got str",
        "diameter: expected a number, got str",
        "label_name: expected a string, got int",
        "overwrite: expected a boolean, got int",
    ]


def test_enum_and_list_length():
    errors = _errors({"channels": {"mode": "name", "identifiers": []}})
    assert sorted(errors) == [
        "channels.identifiers: at least 1 item(s) expected",
        "channels.mode: must be one of 'label', 'index'",
    ]


def test_nullable_and_minimum():
    assert _errors({"channels": {"identifiers": ["a"]}, "level": None}) == []
    errors = _errors({"channels": {"identifiers": ["a"]}, "level": -1})
    assert errors == ["level: must be >= 0"]


def test_unknown_nested_field():
    errors = _errors({"channels": {"identifier": ["a"]}})
    assert sorted(errors) == [
        "channels.identifier: unknown argument (did you mean 'identifiers'?)",
        "channels.identifiers: required",
    ]


def test_discriminated_variant():
    errors = _errors({"channels": {"identifiers": ["a"]},
                      "masking": {"mode": "Create", "table_name": 1}})
    assert errors == ["masking.table_name: expected a string, got int"]
    errors = _errors({"channels": {"identifiers": ["a"]},
                      "masking": {"mode": "Other"}})
    assert errors == ["masking.mode: must be one of 'Create', 'Skip'"]


def test_integer_is_not_bool():
    errors = _errors({"channels": {"identifiers": ["a"]}, "level": True})
    assert errors == ["level: expected an integer or None, got bool"]


def test_reserved_can_be_allowed():
    assert validate({"zarr_url": "/a", "channels": {"identifiers": ["a"]}},
                    SCHEMA, reserved=()) == []


def test_errors_are_schema_errors():
    (error,) = validate({}, SCHEMA)
    assert isinstance(error, SchemaError) and error.path == "channels"


@pytest.mark.parametrize("prop, expected", [
    ("channels", "Channels"),
    ("label_name", "str"),
    ("level", "int | None"),
    ("diameter", "float"),
    ("overwrite", "bool"),
    ("masking", "Create | Skip"),
])
def test_type_name(prop, expected):
    assert type_name(SCHEMA["properties"][prop], DEFS) == expected


def test_type_name_enum_and_list():
    assert type_name(DEFS["Channels"]["properties"]["mode"], DEFS) == \
        "'label' | 'index'"
    assert type_name(DEFS["Channels"]["properties"]["identifiers"], DEFS) == \
        "list[str]"
