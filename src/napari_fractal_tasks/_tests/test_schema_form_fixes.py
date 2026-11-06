"""Form fixes found while testing Fractal task packages (schemas trimmed from
the packages' manifests)."""

import json
from pathlib import Path

import pytest

from workflow_tasks_core.schema import SchemaError

pytest.importorskip("qtpy")
from napari_fractal_tasks._schema_form import (  # noqa: E402
    ListField,
    MapField,
    ObjectField,
    SchemaForm,
)

DATA = Path(__file__).parent / "data"

ROI_PATTERN = (
    '^\\{\\s*(?:"\\w+"\\s*:\\s*(?:"[^"]*"|\\d+(?:\\.\\d+)?)\\s*,?\\s*)+\\}$'
)


def _form(qtbot, schema):
    form = SchemaForm(schema)
    qtbot.addWidget(form)
    return form


# 1. fractal-helper-tasks "ROI Selection": string items containing commas
@pytest.fixture
def roi_schema():
    return {
        "type": "object",
        "properties": {
            "zarr_url": {"type": "string"},
            "roi_corners": {
                "type": "array",
                "items": {"type": "string", "pattern": ROI_PATTERN},
            },
            "output_table_name": {"type": "string", "default": "user_ROIs"},
        },
        "required": ["zarr_url", "roi_corners"],
    }


def test_list_json_strings_with_commas(qtbot, roi_schema):
    form = _form(qtbot, roi_schema)
    field = form.fields["roi_corners"]
    roi = '{"name": "roiA", "x1": 10, "y1": 10, "x2": 200, "y2": 150}'

    # A JSON list of strings
    field.edit.setText(json.dumps([roi]))
    assert form.get_args() == {"roi_corners": [roi]}

    # JSON objects (as pasted from the Vizarr ROI plugin) become JSON strings
    field.edit.setText(roi + ", " + roi.replace("roiA", "roiB"))
    out = form.get_args()["roi_corners"]
    assert [json.loads(x)["name"] for x in out] == ["roiA", "roiB"]
    field.edit.setText("[" + roi + "]")
    assert form.get_args() == {"roi_corners": [roi]}

    # set_value round-trips strings that contain commas
    form.set_values({"roi_corners": [roi, roi]})
    assert form.get_args() == {"roi_corners": [roi, roi]}

    # Items not matching the pattern / broken JSON: error naming the field
    field.edit.setText("x1")
    with pytest.raises(SchemaError, match="roi_corners.*pattern"):
        form.get_args()
    field.edit.setText('[{"name": "a"')
    with pytest.raises(SchemaError, match="roi_corners.*JSON"):
        form.get_args()


def test_list_json_and_comma_forms(qtbot):
    schema = {
        "type": "object",
        "properties": {
            "names": {"type": "array", "items": {"type": "string"}},
            "sizes": {"type": "array", "items": {"type": "integer"}},
        },
    }
    form = _form(qtbot, schema)
    names, sizes = form.fields["names"], form.fields["sizes"]
    names.edit.setText('["a, b", "c"]')
    sizes.edit.setText("[1, 2]")
    assert form.get_args() == {"names": ["a, b", "c"], "sizes": [1, 2]}
    names.edit.setText("DAPI, nanog,")  # comma separated still works
    assert form.get_args()["names"] == ["DAPI", "nanog"]
    sizes.edit.setText('[1, "x"]')
    with pytest.raises(SchemaError, match="sizes"):
        form.get_args()
    sizes.edit.setText("[true]")
    with pytest.raises(SchemaError, match="sizes"):
        form.get_args()
    names.set_value(["a, b", "c"])
    assert names.edit.text() == '["a, b", "c"]'
    names.set_value(["DAPI", "nanog"])
    assert names.edit.text() == "DAPI, nanog"


# 2. fractal-napari-workflows: optional nested model left empty
NAPARI_DEFS = {
    "ChannelInputModel": {
        "properties": {
            "wavelength_id": {"type": "string"},
            "label": {"type": "string"},
        },
        "type": "object",
    },
    "NapariWorkflowsInput": {
        "properties": {
            "type": {"enum": ["image", "label"], "type": "string"},
            "label_name": {"type": "string"},
            "channel": {"allOf": [{"$ref": "#/$defs/ChannelInputModel"}]},
        },
        "required": ["type"],
        "type": "object",
    },
}


def test_optional_model_left_empty_is_omitted(qtbot):
    schema = {
        "type": "object",
        "properties": {
            "input_specs": {
                "type": "object",
                "additionalProperties": {"$ref": "#/$defs/NapariWorkflowsInput"},
            }
        },
        "required": ["input_specs"],
        "$defs": NAPARI_DEFS,
    }
    form = _form(qtbot, schema)
    specs = form.fields["input_specs"]
    assert isinstance(specs, MapField)
    form.set_values(
        {
            "input_specs": {
                "label_img": {"type": "label", "label_name": "nuclei"},
                "img": {"type": "image", "channel": {"label": "DAPI"}},
            }
        }
    )
    assert form.get_args() == {
        "input_specs": {
            "label_img": {"type": "label", "label_name": "nuclei"},
            "img": {"type": "image", "channel": {"label": "DAPI"}},
        }
    }


# 3. abbott "Compute Channel Registration (elastix)": optional allOf[$ref]
# whose model has required fields
@pytest.fixture
def abbott_schema():
    return {
        "type": "object",
        "properties": {
            "reference_channel": {"$ref": "#/$defs/ChannelSelectionModel"},
            "calculate_on_labels": {"allOf": [{"$ref": "#/$defs/LabelInputModel"}]},
            "level_path": {"default": 2, "type": "integer"},
        },
        "required": ["reference_channel"],
        "$defs": {
            "ChannelSelectionModel": {
                "properties": {
                    "mode": {"default": "label", "enum": ["label", "index"]},
                    "identifier": {"type": "string"},
                },
                "required": ["identifier"],
                "type": "object",
            },
            "LabelInputModel": {
                "properties": {
                    "reference_label_name": {"type": "string"},
                    "align_label_names": {"items": {"type": "string"}, "type": "array"},
                },
                "required": ["reference_label_name", "align_label_names"],
                "type": "object",
            },
        },
    }


def test_optional_ref_without_default(qtbot, abbott_schema):
    form = _form(qtbot, abbott_schema)
    labels = form.fields["calculate_on_labels"]
    assert isinstance(labels, ObjectField)
    form.set_values({"reference_channel": {"identifier": "DAPI"}})
    # Untouched: left out, its required fields are not checked
    assert form.get_args() == {"reference_channel": {"mode": "label", "identifier": "DAPI"}}

    # Partly filled: now its required fields are checked
    labels.form.fields["reference_label_name"].edit.setText("nuclei")
    with pytest.raises(SchemaError, match="calculate_on_labels.align_label_names"):
        form.get_args()
    labels.form.fields["align_label_names"].edit.setText("cells")
    assert form.get_args()["calculate_on_labels"] == {
        "reference_label_name": "nuclei",
        "align_label_names": ["cells"],
    }

    # Cleared again: left out again
    labels.form.fields["reference_label_name"].edit.setText("")
    labels.form.fields["align_label_names"].edit.setText("")
    assert "calculate_on_labels" not in form.get_args()


def test_required_model_still_validated(qtbot, abbott_schema):
    form = _form(qtbot, abbott_schema)
    with pytest.raises(SchemaError, match="reference_channel.identifier: required"):
        form.get_args()


# 4. zmb-fractal-tasks "Delete labels": required, nullable, no default
def test_required_nullable_sent_as_null(qtbot):
    schema = {
        "type": "object",
        "properties": {
            "labels_to_delete": {
                "anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}]
            },
            "name": {"type": ["string", "null"]},
            "optional_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["labels_to_delete", "name"],
    }
    form = _form(qtbot, schema)
    assert form.get_args() == {"labels_to_delete": None, "name": None}
    form.fields["labels_to_delete"].edit.setText("cyto")
    form.fields["name"].edit.setText("x")
    assert form.get_args() == {"labels_to_delete": ["cyto"], "name": "x"}
    form.fields["labels_to_delete"].edit.setText("[]")
    assert form.get_args()["labels_to_delete"] == []
    form.set_values({"labels_to_delete": None})
    assert form.get_args()["labels_to_delete"] is None


# 5. abbott-segmentation-tasks Stardist: identifiers default [] but minItems 1
def test_min_items_checked_for_empty_list(qtbot):
    schema = {
        "type": "object",
        "properties": {
            "channel": {"$ref": "#/$defs/StardistChannel"},
        },
        "required": ["channel"],
        "$defs": {
            "StardistChannel": {
                "properties": {
                    "mode": {"default": "label", "enum": ["label", "index"]},
                    "identifiers": {
                        "default": [],
                        "items": {"type": "string"},
                        "minItems": 1,
                        "type": "array",
                    },
                },
                "type": "object",
            }
        },
    }
    form = _form(qtbot, schema)
    with pytest.raises(SchemaError, match="channel.identifiers: needs at least 1"):
        form.get_args()
    ids = form.fields["channel"].form.fields["identifiers"]
    assert isinstance(ids, ListField)
    ids.edit.setText("DAPI")
    assert form.get_args() == {"channel": {"mode": "label", "identifiers": ["DAPI"]}}


# Review Focus of docs/superpowers/plans/2026-11-03-cellpose-sam-0.2.3-compat.md
@pytest.fixture
def sam_form(qtbot):
    with open(DATA / "cellpose_sam_0.2.3_args_schema.json") as f:
        schema = json.load(f)["args_schema_parallel"]
    form = _form(qtbot, schema)
    form.fields["channels"].form.fields["identifiers"].edit.setText("DAPI,")
    return form


def test_sam_review_focus(sam_form):
    # 3. trailing comma
    args = sam_form.get_args()
    assert args["channels"]["identifiers"] == ["DAPI"]
    ids = sam_form.fields["channels"].form.fields["identifiers"]
    ids.edit.setText("DAPI, nanog")
    assert sam_form.get_args()["channels"]["identifiers"] == ["DAPI", "nanog"]
    # 2. untouched / cleared defaults are not sent (no nulls)
    assert all(v is not None for v in args.values())
    assert set(args) == {"channels"}
    # 1. Python-style quotes in a JSON-ish list: error naming the field
    ids.edit.setText("['DAPI']")
    with pytest.raises(SchemaError, match="channels.identifiers"):
        sam_form.get_args()
