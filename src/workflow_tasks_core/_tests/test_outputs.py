"""Working out a task's outputs from the arguments it was run with."""

from workflow_tasks_core.outputs import (
    channel_identifier,
    expected_image_urls,
    expected_label_names,
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
