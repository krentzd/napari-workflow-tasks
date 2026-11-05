import json
from pathlib import Path

import pytest

from workflow_tasks_core.schema import SchemaError

pytest.importorskip("qtpy")
from napari_workflow_tasks._schema_form import (  # noqa: E402
    ListField,
    MapField,
    ModelListField,
    NumberField,
    OptionalField,
    SchemaForm,
    UnionField,
)

DATA = Path(__file__).parent / "data"


@pytest.fixture
def sam_schema():
    with open(DATA / "cellpose_sam_0.2.3_args_schema.json") as f:
        return json.load(f)["args_schema_parallel"]


def test_cellpose_sam_form(qtbot, sam_schema):
    form = SchemaForm(sam_schema)
    qtbot.addWidget(form)

    # Widgets chosen from the schema
    assert "zarr_url" not in form.fields  # reserved
    assert isinstance(form.fields["iterator_configuration"], OptionalField)
    assert isinstance(form.fields["create_masking_roi_table"], UnionField)
    pre = form.fields["pre_post_process"].form.fields["pre_process"]
    assert isinstance(pre, ModelListField)
    adv = form.fields["advanced_parameters"].form.fields
    assert isinstance(adv["flow_threshold"], NumberField)

    # Required channel identifiers
    with pytest.raises(SchemaError, match="identifiers"):
        form.get_args()
    ids = form.fields["channels"].form.fields["identifiers"]
    assert isinstance(ids, ListField)
    ids.edit.setText("DAPI")

    # Untouched defaults are not sent (the task applies its own)
    assert form.get_args() == {
        "channels": {"mode": "label", "identifiers": ["DAPI"], "skip_if_missing": False}
    }

    # Edits are sent with the right JSON types
    adv["flow_threshold"].spin.setValue(0.6)
    pre.add_row()
    args = form.get_args()
    assert args["advanced_parameters"]["flow_threshold"] == 0.6
    assert args["pre_post_process"]["pre_process"][0]["type"] == "Gaussian Blur Filter"

    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate({**args, "zarr_url": "/x.zarr/B/03/0"}, sam_schema)

    form.reset()
    ids.edit.setText("DAPI")
    assert list(form.get_args()) == ["channels"]


def test_map_and_optional_fields(qtbot):
    schema = {
        "type": "object",
        "properties": {
            "zarr_url": {"type": "string"},
            "profiles": {"type": "object", "additionalProperties": {"type": "string"}},
            "diameter": {"anyOf": [{"type": "number"}, {"type": "null"}], "default": None},
            "level": {"type": "integer", "default": 0, "minimum": 0},
        },
        "required": ["zarr_url", "profiles"],
    }
    form = SchemaForm(schema)
    qtbot.addWidget(form)
    profiles = form.fields["profiles"]
    assert isinstance(profiles, MapField)
    profiles.add_row(("A01_C01", "/profiles/a.zarr"))
    form.fields["diameter"].edit.setText("30")
    assert form.get_args() == {"profiles": {"A01_C01": "/profiles/a.zarr"}, "diameter": 30.0}
    assert form.fields["level"].spin.minimum() == 0
