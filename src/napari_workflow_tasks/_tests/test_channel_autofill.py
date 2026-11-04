"""Channel identifiers and label names follow the selected input channel."""

import json
from pathlib import Path

import pytest

pytest.importorskip("napari")
from napari_workflow_tasks._schema_form import SchemaForm  # noqa: E402
from napari_workflow_tasks._widget import (  # noqa: E402
    ChannelAutofill,
    _input_channel,
)

DATA = Path(__file__).parent / "data"


@pytest.fixture
def sam_form(qtbot):
    """Cellpose SAM 0.2.3: channels.identifiers + label_name template."""
    schema = json.loads(
        (DATA / "cellpose_sam_0.2.3_args_schema.json").read_text()
    )["args_schema_parallel"]
    form = SchemaForm(schema)
    qtbot.addWidget(form)
    return form


def _text(form, *path):
    fields = form.fields
    for key in path[:-1]:
        fields = fields[key].form.fields
    return fields[path[-1]].edit.text()


def _edit(form, value, *path):
    fields = form.fields
    for key in path[:-1]:
        fields = fields[key].form.fields
    fields[path[-1]].edit.setText(value)


# --------------------------------------------------------------------------
# _input_channel: which channel an input layer shows
# --------------------------------------------------------------------------


def test_input_channel_matches_omero_label():
    assert _input_channel("DAPI", ["DAPI", "nanog"]) == "DAPI"


def test_input_channel_strips_output_suffix():
    # layers of task outputs are named "<channel> [<image>]"
    assert _input_channel("DAPI [0_gaussian_blur]", ["DAPI"]) == "DAPI"


def test_input_channel_not_a_channel():
    assert _input_channel("nuclei", ["DAPI"]) is None
    assert _input_channel("", ["DAPI"]) is None


def test_input_channel_without_channel_metadata_uses_layer_name():
    assert _input_channel("img", []) == "img"


# --------------------------------------------------------------------------
# ChannelAutofill
# --------------------------------------------------------------------------


def test_fills_identifier_and_label_name(sam_form):
    ChannelAutofill(sam_form.fields).set_channel("DAPI")
    args = sam_form.get_args()
    assert args["channels"]["identifiers"] == ["DAPI"]
    assert args["label_name"] == "DAPI_segmented"


def test_follows_channel_changes(sam_form):
    autofill = ChannelAutofill(sam_form.fields)
    autofill.set_channel("DAPI")
    autofill.set_channel("nanog")
    assert _text(sam_form, "channels", "identifiers") == "nanog"
    assert _text(sam_form, "label_name") == "nanog_segmented"


def test_no_channel_leaves_fields(sam_form):
    autofill = ChannelAutofill(sam_form.fields)
    autofill.set_channel("DAPI")
    autofill.set_channel(None)
    assert _text(sam_form, "channels", "identifiers") == "DAPI"
    assert _text(sam_form, "label_name") == "DAPI_segmented"


def test_keeps_user_edits(sam_form):
    autofill = ChannelAutofill(sam_form.fields)
    autofill.set_channel("DAPI")
    _edit(sam_form, "nuclei", "label_name")
    _edit(sam_form, "DAPI, nanog", "channels", "identifiers")
    autofill.set_channel("nanog")
    assert _text(sam_form, "label_name") == "nuclei"
    assert _text(sam_form, "channels", "identifiers") == "DAPI, nanog"


def test_label_name_follows_typed_identifier(sam_form):
    autofill = ChannelAutofill(sam_form.fields)
    autofill.set_channel("DAPI")
    _edit(sam_form, "nanog", "channels", "identifiers")
    assert _text(sam_form, "label_name") == "nanog_segmented"


def test_cleared_field_is_filled_again(sam_form):
    autofill = ChannelAutofill(sam_form.fields)
    autofill.set_channel("DAPI")
    _edit(sam_form, "", "label_name")
    autofill.set_channel("nanog")
    assert _text(sam_form, "label_name") == "nanog_segmented"


def _schema(label_prop, chan=None, required=("channel",)):
    chan = chan or {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "default": "label"},
            "identifiers": {"type": "array", "items": {"type": "string"},
                            "default": [], "minItems": 1},
        },
    }
    return {
        "type": "object",
        "properties": {
            "channel": {"$ref": "#/$defs/Chan"},
            "extra": {"anyOf": [{"$ref": "#/$defs/Chan"}, {"type": "null"}]},
            "other": {"type": "array", "items": {"type": "string"},
                      "default": []},
            "output_label_name": label_prop,
        },
        "required": list(required),
        "$defs": {"Chan": chan},
    }


def test_optional_models_and_other_lists_untouched(qtbot):
    form = SchemaForm(_schema({"type": "string", "default": "x"}))
    qtbot.addWidget(form)
    ChannelAutofill(form.fields).set_channel("DAPI")
    args = form.get_args()
    assert args["channel"]["identifiers"] == ["DAPI"]
    assert "extra" not in args or args["extra"] in (None, {})
    assert args.get("other", []) == []


def test_fixed_default_label_name_untouched(qtbot):
    form = SchemaForm(_schema({"type": "string", "default": "nuclei"}))
    qtbot.addWidget(form)
    ChannelAutofill(form.fields).set_channel("DAPI")
    assert _text(form, "output_label_name") == "nuclei"


def test_optional_label_name_without_default_filled(qtbot):
    # fractal-tasks-core: output_label_name: Optional[str] = None
    form = SchemaForm(_schema(
        {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    ))
    qtbot.addWidget(form)
    ChannelAutofill(form.fields).set_channel("DAPI")
    assert form.get_args()["output_label_name"] == "DAPI_segmented"


# --------------------------------------------------------------------------
# In the widget: switching the input layer, closing the tab
# --------------------------------------------------------------------------


def test_widget_input_layer_drives_task_tab(qtbot, tmp_path):
    import numpy as np
    from napari.components import ViewerModel

    from napari_workflow_tasks._widget import TasksQWidget

    zarr = tmp_path / "img.zarr"
    zarr.mkdir()
    (zarr / ".zattrs").write_text(json.dumps(
        {"omero": {"channels": [{"label": "DAPI"}, {"label": "nanog"}]}}
    ))
    schema = json.loads(
        (DATA / "cellpose_sam_0.2.3_args_schema.json").read_text()
    )["args_schema_parallel"]

    viewer = ViewerModel()
    widget = TasksQWidget(viewer)
    qtbot.addWidget(widget)
    for name in ("DAPI", "nanog"):
        viewer.add_image(np.zeros((4, 4)), name=name,
                         metadata={"zarr_url": str(zarr)})
    widget._image_layers.setCurrentText("DAPI")

    widget.task_manager.add_task(
        name="SAM", package_name="pkg==1", parent_dir=str(tmp_path),
        executable_parallel="x.py", properties=schema["properties"],
        defs=schema["$defs"], required=schema["required"],
        type="object", title="SAM",
    )
    widget._add_task_tab("SAM")
    form = widget._task_ui["SAM"]["form"]
    assert _text(form, "channels", "identifiers") == "DAPI"
    assert _text(form, "label_name") == "DAPI_segmented"

    widget._image_layers.setCurrentText("nanog")
    assert _text(form, "channels", "identifiers") == "nanog"
    assert _text(form, "label_name") == "nanog_segmented"

    # closed tab: switching the input must not touch its deleted widgets
    from qtpy.QtCore import QCoreApplication, QEvent

    widget._close_tab("SAM")
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    widget._image_layers.setCurrentText("DAPI")


# --------------------------------------------------------------------------
# Single-channel tasks (StarDist, microSAM, Threshold Segmentation):
# channel.identifier is a string
# --------------------------------------------------------------------------


def _single_channel_schema(masking_required=False):
    return {
        "type": "object",
        "properties": {
            "zarr_url": {"type": "string"},
            "channel": {"$ref": "#/$defs/ChannelInput"},
            "masking": {"$ref": "#/$defs/MaskingConfig"},
            "label_name": {"type": "string",
                           "default": "{channel_identifier}_stardist_segmented"},
        },
        "required": ["zarr_url", "channel"]
        + (["masking"] if masking_required else []),
        "$defs": {
            "ChannelInput": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "default": "label"},
                    "identifier": {"type": "string"},
                },
                "required": ["identifier"],
            },
            "MaskingConfig": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "default": "Label Name"},
                    "identifier": {"type": "string"},
                },
                "required": ["identifier"],
            },
        },
    }


def test_single_identifier_and_label_name(qtbot):
    form = SchemaForm(_single_channel_schema())
    qtbot.addWidget(form)
    autofill = ChannelAutofill(form.fields)
    autofill.set_channel("DAPI")
    args = form.get_args()
    assert args["channel"]["identifier"] == "DAPI"
    assert args["label_name"] == "DAPI_stardist_segmented"

    autofill.set_channel("nanog")
    assert _text(form, "channel", "identifier") == "nanog"
    assert _text(form, "label_name") == "nanog_stardist_segmented"


def test_single_identifier_keeps_edits_and_drives_label(qtbot):
    form = SchemaForm(_single_channel_schema())
    qtbot.addWidget(form)
    autofill = ChannelAutofill(form.fields)
    autofill.set_channel("DAPI")
    _edit(form, "Lamin B1", "channel", "identifier")
    assert _text(form, "label_name") == "Lamin B1_stardist_segmented"
    autofill.set_channel("nanog")
    assert _text(form, "channel", "identifier") == "Lamin B1"


def test_masking_identifier_not_a_channel(qtbot):
    # "identifier" of a masking model names a label/table, not a channel
    form = SchemaForm(_single_channel_schema(masking_required=True))
    qtbot.addWidget(form)
    ChannelAutofill(form.fields).set_channel("DAPI")
    assert _text(form, "masking", "identifier") == ""
