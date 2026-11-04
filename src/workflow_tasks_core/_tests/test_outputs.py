"""Working out a task's outputs from the arguments it was run with."""

import json

from workflow_tasks_core.outputs import (
    channel_identifier,
    expected_image_urls,
    expected_label_names,
    find_task_outputs,
    new_image_urls,
    new_label_names,
    reported_image_urls,
    rewritten_inputs,
)

LABEL_PROPS = {
    "zarr_url": {"type": "string"},
    "label_name": {"type": "string",
                   "default": "{channel_identifier}_stardist_segmented"},
}


# --------------------------------------------------------------------------
# channel_identifier
# --------------------------------------------------------------------------


def test_channel_identifier_single():
    assert channel_identifier({"channel": {"identifier": "nanog"}}) == "nanog"


def test_channel_identifier_list_uses_first():
    args = {"channels": {"identifiers": ["DAPI", "nanog"]}}
    assert channel_identifier(args) == "DAPI"


def test_channel_identifier_ignores_masking_identifier():
    args = {"iterator_configuration": {"masking": {"identifier": "nuclei"}}}
    assert channel_identifier(args) is None


# --------------------------------------------------------------------------
# expected_label_names
# --------------------------------------------------------------------------


def test_label_from_default_template_and_channel():
    # label_name left at its default is not sent to the task
    args = {"zarr_url": "/d/0", "channel": {"identifier": "nanog"}}
    assert expected_label_names(args, LABEL_PROPS) == ["nanog_stardist_segmented"]


def test_label_typed_template():
    args = {"channel": {"identifier": "DAPI"}, "label_name": "{channel_identifier}_x"}
    assert expected_label_names(args, LABEL_PROPS) == ["DAPI_x"]


def test_label_plain_name():
    args = {"channel": {"identifier": "DAPI"}, "label_name": "nuclei"}
    assert expected_label_names(args, LABEL_PROPS) == ["nuclei"]


def test_output_label_name_key():
    props = {"output_label_name": {"type": "string",
                                   "default": "{channel_identifier}_segmented"}}
    args = {"channel": {"identifier": "DAPI"}}
    assert expected_label_names(args, props) == ["DAPI_segmented"]


def test_label_template_without_channel_is_unknown():
    assert expected_label_names({}, LABEL_PROPS) == []


def test_no_label_argument():
    assert expected_label_names({"channel": {"identifier": "DAPI"}}, {}) == []


def test_label_name_none():
    props = {"label_name": {"anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None}}
    assert expected_label_names({"channel": {"identifier": "D"}}, props) == []


# --------------------------------------------------------------------------
# expected_image_urls
# --------------------------------------------------------------------------


def test_image_from_suffix():
    # fractal-tasks-template Gaussian Blur
    props = {"output_image_suffix": {"type": "string", "default": "gaussian_blur"}}
    assert expected_image_urls({}, props, "/plate.zarr/B/03/0") == [
        "/plate.zarr/B/03/0_gaussian_blur"
    ]


def test_image_from_suffix_keeps_zarr_extension():
    props = {"output_image_suffix": {"type": "string", "default": "blur"}}
    assert expected_image_urls({}, props, "/data/img.zarr") == ["/data/img_blur.zarr"]


def test_image_suffix_overwrite_input_is_the_input():
    props = {"output_image_suffix": {"type": "string", "default": "blur"},
             "overwrite_input": {"type": "boolean", "default": False}}
    assert expected_image_urls({"overwrite_input": True}, props, "/d/0") == []


def test_image_from_name_template():
    # fractal-tasks-core Illumination Correction
    props = {"output_image_name": {"type": "string",
                                   "default": "{image_name}_illum_corr"}}
    assert expected_image_urls({}, props, "/plate.zarr/B/03/0") == [
        "/plate.zarr/B/03/0_illum_corr"
    ]


def test_image_name_template_with_unknown_placeholder_skipped():
    # Project Image: {method} is the method's abbreviation, only the task
    # knows it; the image is found through out_file.json instead
    props = {"output_image_name": {"type": "string",
                                   "default": "{image_name}_{method}"}}
    assert expected_image_urls({}, props, "/d/0") == []


# --------------------------------------------------------------------------
# reported_image_urls (the task's --out-json)
# --------------------------------------------------------------------------


def test_reported_images(tmp_path):
    out = tmp_path / "out.json"
    out.write_text(json.dumps({"image_list_updates": [
        {"zarr_url": "/d/0_blur", "origin": "/d/0"},
        {"zarr_url": "/d/0/"},  # the input itself
    ]}))
    assert reported_image_urls(str(out), "/d/0") == ["/d/0_blur"]


def test_reported_images_null_or_missing(tmp_path):
    out = tmp_path / "out.json"
    out.write_text("null")
    assert reported_image_urls(str(out), "/d/0") == []
    assert reported_image_urls(str(tmp_path / "nope.json"), "/d/0") == []


# --------------------------------------------------------------------------
# Outputs found on disk after a run
# --------------------------------------------------------------------------


def _image(path, labels=()):
    path.mkdir(parents=True)
    (path / "labels").mkdir()
    (path / "labels" / ".zattrs").write_text(
        json.dumps({"labels": list(labels)}))
    return str(path)


def test_find_task_outputs(tmp_path):
    url = _image(tmp_path / "img.zarr", ["old", "DAPI_seg", "new"])
    _image(tmp_path / "img_blur.zarr")
    out = tmp_path / "out.json"
    out.write_text(json.dumps({"image_list_updates": [
        {"zarr_url": "/elsewhere/new.zarr"}]}))
    props = {"label_name": {"type": "string"},
             "output_image_suffix": {"type": "string", "default": "blur"},
             "output_image_name": {"type": "string", "default": "{image_name}_x"}}
    args = {"zarr_url": url, "label_name": "DAPI_seg"}

    outputs = find_task_outputs(args, props, str(out), {"old", "DAPI_seg"})
    # named by the arguments, or new
    assert outputs.label_names == {"DAPI_seg", "new"}
    assert outputs.image_urls == [
        "/elsewhere/new.zarr", str(tmp_path / "img_blur.zarr")]
    assert outputs.missing == [f"image {tmp_path / 'img.zarr_x'}"]
    assert outputs.input_rewritten is False


def test_find_task_outputs_input_rewritten(tmp_path):
    # e.g. Apply Registration to Image with overwrite_input: the task
    # replaces the input image and reports it in image_list_updates
    url = _image(tmp_path / "img.zarr")
    out = tmp_path / "out.json"
    out.write_text(json.dumps({"image_list_updates": [{"zarr_url": url}]}))
    outputs = find_task_outputs({"zarr_url": url}, {}, str(out), set())
    assert outputs.input_rewritten is True
    assert outputs.image_urls == []


def test_find_task_outputs_missing_label(tmp_path):
    url = _image(tmp_path / "img.zarr")
    args = {"zarr_url": url, "label_name": "nuclei"}
    outputs = find_task_outputs(args, LABEL_PROPS, str(tmp_path / "no.json"),
                                set())
    assert outputs.label_names == set()
    assert outputs.image_urls == []
    assert outputs.missing == [f"label 'nuclei' in {url}"]


def test_new_label_names(tmp_path):
    url = _image(tmp_path / "0", ["old", "nanog_stardist_segmented", "new"])
    args = {"channel": {"identifier": "nanog"}}
    names = new_label_names(url, {"old", "nanog_stardist_segmented"}, args,
                            LABEL_PROPS)
    assert names == {"new", "nanog_stardist_segmented"}


def test_new_image_urls():
    updates = [{"zarr_url": "/d/0/"}, {"zarr_url": "/d/1"},
               {"zarr_url": "/d/1"}, {"origin": "/d/0"}]
    assert new_image_urls(updates, ["/d/0"]) == ["/d/1"]


# --------------------------------------------------------------------------
# Tables named by the arguments
# --------------------------------------------------------------------------

MASKING_DEFS = {
    "Create": {"type": "object", "properties": {
        "mode": {"const": "Create", "default": "Create"},
        "table_name": {"type": "string", "default": "{label_name}_masking_ROI_table"},
    }},
    "Skip": {"type": "object", "properties": {
        "mode": {"const": "Skip", "default": "Skip"}}},
}
MASKING_SCHEMA = {
    "properties": {
        "channels": {"type": "object"},
        "label_name": {"type": "string", "default": "{channel_identifier}_segmented"},
        "create_masking_roi_table": {
            "oneOf": [{"$ref": "#/$defs/Create"}, {"$ref": "#/$defs/Skip"}],
            "discriminator": {"propertyName": "mode", "mapping": {
                "Create": "#/$defs/Create", "Skip": "#/$defs/Skip"}},
            "default": {"mode": "Skip"},
        },
        "output_table_name": {"type": "string"},
        "input_roi_table": {"type": "string", "default": "FOV_ROI_table"},
    },
    "$defs": MASKING_DEFS,
}


def test_tables_from_nested_default_template():
    from workflow_tasks_core.outputs import expected_table_names
    args = {"channels": {"identifiers": ["DAPI"]},
            "create_masking_roi_table": {"mode": "Create"}}
    assert expected_table_names(args, MASKING_SCHEMA) == [
        "DAPI_segmented_masking_ROI_table"]


def test_tables_skip_variant_and_inputs_ignored():
    from workflow_tasks_core.outputs import expected_table_names
    args = {"channels": {"identifiers": ["DAPI"]}}
    assert expected_table_names(args, MASKING_SCHEMA) == []


def test_tables_output_table_name():
    from workflow_tasks_core.outputs import expected_table_names
    args = {"output_table_name": "features",
            "create_masking_roi_table": {"mode": "Create", "table_name": "m"}}
    assert expected_table_names(args, MASKING_SCHEMA) == ["m", "features"]


def test_rewritten_inputs():
    updates = [{"zarr_url": "/d/0/"}, {"zarr_url": "/d/1_registered",
                                       "origin": "/d/1"}]
    assert rewritten_inputs(updates, ["/d/0", "/d/1"]) == ["/d/0"]
    assert rewritten_inputs([], ["/d/0"]) == []
