"""Arguments whose default names other arguments ("{input_roi_table}_
registered") follow them."""

import pytest

pytest.importorskip("napari")

from napari_workflow_tasks._autofill import (  # noqa: E402
    ChannelAutofill,
    PlaceholderAutofill,
)
from napari_workflow_tasks._schema_form import SchemaForm  # noqa: E402

# fractal-tasks-core 2.1.0, Find Registration Consensus
CONSENSUS = {
    "type": "object",
    "properties": {
        "zarr_url": {"type": "string"},
        "input_roi_table": {"type": "string", "default": "FOV_ROI_table"},
        "registered_roi_table": {"type": "string",
                                 "default": "{input_roi_table}_registered"},
    },
    "required": ["zarr_url"],
}
# fractal-tasks-core 2.1.0, Threshold Segmentation (simplified)
THRESHOLD = {
    "type": "object",
    "properties": {
        "zarr_url": {"type": "string"},
        "channel": {"$ref": "#/$defs/Channel"},
        "output_label_name": {"type": "string",
                              "default": "{channel_identifier}_segmented"},
        "create_masking_roi_table": {
            "oneOf": [{"$ref": "#/$defs/Skip"}, {"$ref": "#/$defs/Create"}],
            "discriminator": {"propertyName": "mode", "mapping": {
                "Skip": "#/$defs/Skip", "Create": "#/$defs/Create"}},
            "default": {"mode": "Skip"},
        },
        "output_image_name": {"type": "string",
                              "default": "{image_name}_thresholded"},
    },
    "required": ["zarr_url", "channel"],
    "$defs": {
        "Channel": {"type": "object", "required": ["identifier"],
                    "properties": {"identifier": {"type": "string"}}},
        "Skip": {"type": "object", "properties": {
            "mode": {"const": "Skip", "type": "string", "default": "Skip"}}},
        "Create": {"type": "object", "properties": {
            "mode": {"const": "Create", "type": "string", "default": "Create"},
            "table_name": {"type": "string",
                           "default": "{output_label_name}_masking_ROI_table"}}},
    },
}


def _form(qtbot, schema):
    form = SchemaForm(schema)
    qtbot.addWidget(form)
    return form


def test_follows_the_argument_it_names(qtbot):
    form = _form(qtbot, CONSENSUS)
    autofill = PlaceholderAutofill(form.fields)  # noqa: F841 - keep it alive
    registered = form.fields["registered_roi_table"]
    assert registered.value() == "FOV_ROI_table_registered"  # from the default
    form.fields["input_roi_table"].set_value("my_rois")
    assert registered.value() == "my_rois_registered"


def test_keeps_what_the_user_typed(qtbot):
    form = _form(qtbot, CONSENSUS)
    autofill = PlaceholderAutofill(form.fields)
    form.fields["registered_roi_table"].edit.setText("mine")
    form.fields["input_roi_table"].set_value("my_rois")
    assert form.fields["registered_roi_table"].value() == "mine"
    form.reset()
    autofill.fill()
    assert form.fields["registered_roi_table"].value() == "FOV_ROI_table_registered"


def test_inside_a_model_and_after_the_channel(qtbot):
    form = _form(qtbot, THRESHOLD)
    channels = ChannelAutofill(form.fields)  # fills output_label_name ...
    autofill = PlaceholderAutofill(form.fields)  # noqa: F841 - ... and that the table name
    channels.set_channel("DAPI")
    masking = form.fields["create_masking_roi_table"]
    masking.set_value({"mode": "Create"})
    assert masking.value()["table_name"] == "DAPI_segmented_masking_ROI_table"
    # {image_name} is no argument: the task fills it per image
    assert form.fields["output_image_name"].edit.text() == "{image_name}_thresholded"


def test_in_a_task_tab(qtbot, fake_package):
    """The ROI table saved in the tab fills input_roi_table, and
    registered_roi_table follows."""
    from napari.components import ViewerModel

    from napari_workflow_tasks._widget import TasksQWidget

    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    widget.add_package(fake_package([{
        "name": "Consensus", "executable_parallel": "c.py",
        "args_schema_parallel": CONSENSUS}]), "Consensus")
    tab = widget._tabs["Consensus [p==1.0]"]
    assert tab.form.get_args() == {
        "registered_roi_table": "FOV_ROI_table_registered"}
    tab._use_roi_table("my_rois")
    assert tab.form.get_args() == {"input_roi_table": "my_rois",
                                   "registered_roi_table": "my_rois_registered"}
    tab.reset()
    assert tab.form.get_args() == {
        "registered_roi_table": "FOV_ROI_table_registered"}
