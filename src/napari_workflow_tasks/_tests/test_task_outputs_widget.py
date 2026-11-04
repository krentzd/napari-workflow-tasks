"""After a run, the widget shows the outputs named by the task's arguments."""

import stat
import sys

import numpy as np
import pytest

pytest.importorskip("napari")
if sys.platform == "win32":
    pytest.skip("uses a POSIX shell script as fake pixi", allow_module_level=True)

from napari.components import ViewerModel  # noqa: E402
from napari.layers._source import layer_source  # noqa: E402
from napari_ome_zarr._reader import napari_get_reader  # noqa: E402
from ngio import create_ome_zarr_from_array  # noqa: E402

from napari_workflow_tasks import _widget as W  # noqa: E402

# Writes "<label_name with {channel_identifier}>" like the Fractal
# segmentation tasks, unless WRITE is False
TASK = '''
import argparse, json, numpy as np
from ngio import open_ome_zarr_container
p = argparse.ArgumentParser(); p.add_argument("--args-json"); p.add_argument("--out-json")
a = p.parse_args(); args = json.load(open(a.args_json))
if WRITE:
    name = args.get("label_name", "{channel_identifier}_stardist_segmented").format(
        channel_identifier=args["channel"]["identifier"])
    c = open_ome_zarr_container(args["zarr_url"])
    c.derive_label(name, overwrite=True).set_array(np.ones((32, 32), "uint32"))
json.dump(None, open(a.out_json, "w"))
'''

SCHEMA = {
    "properties": {
        "zarr_url": {"type": "string"},
        "channel": {"$ref": "#/$defs/Channel"},
        "label_name": {"type": "string",
                       "default": "{channel_identifier}_stardist_segmented"},
    },
    "required": ["zarr_url", "channel"],
    "$defs": {"Channel": {
        "type": "object",
        "properties": {"identifier": {"type": "string"}},
        "required": ["identifier"],
    }},
}


@pytest.fixture
def setup(qtbot, tmp_path, monkeypatch):
    def make(write=True, task=None):
        url = str(tmp_path / "img.zarr")
        container = create_ome_zarr_from_array(
            url, np.zeros((2, 32, 32), "uint16"), xy_pixelsize=1.0,
            axes_names="cyx", channel_labels=["DAPI", "nanog"], levels=1,
        )
        # left by an earlier run
        container.derive_label("nanog_stardist_segmented").set_array(
            np.zeros((32, 32), "uint32"))
        (tmp_path / "task.py").write_text(task or f"WRITE = {write}\n" + TASK)
        pixi = tmp_path / "pixi"
        pixi.write_text(
            '#!/bin/sh\nwhile [ "$1" != python ]; do shift; done\nshift\n'
            f'exec {sys.executable} "$@"\n')
        pixi.chmod(pixi.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setattr(W, "find_pixi", lambda: str(pixi))
        messages = []
        monkeypatch.setattr(W, "show_info", messages.append)
        monkeypatch.setattr(W, "show_warning", messages.append)

        viewer = ViewerModel()
        with layer_source(path=url, reader_plugin="napari-ome-zarr"):
            for data in napari_get_reader(url)(url):
                viewer._add_layer_from_data(*data)
        widget = W.TasksQWidget(viewer)
        qtbot.addWidget(widget)
        widget._image_layers.setCurrentText("DAPI")
        widget.task_manager.add_task(
            name="SD", package_name="p", parent_dir=str(tmp_path),
            executable_parallel="task.py", properties=SCHEMA["properties"],
            defs=SCHEMA["$defs"], required=SCHEMA["required"],
            type="object", title="SD",
        )
        widget.loaded_packages["p"] = {"pixi_manifest_path": "x"}
        widget._add_task_tab("SD")
        return viewer, widget, messages, tmp_path

    return make


def _run(qtbot, widget):
    widget._execute_task("SD")
    qtbot.waitUntil(lambda: not widget._task_workers, timeout=60000)


def test_rerun_shows_label_named_by_arguments(qtbot, setup):
    # Input layer DAPI, but the task segments nanog; its label already exists
    viewer, widget, messages, _ = setup()
    form = widget._task_ui["SD"]["form"]
    form.set_values({
        "channel": {"identifier": "nanog"},
        "label_name": "{channel_identifier}_stardist_segmented",
    })
    before = viewer.layers["nanog_stardist_segmented"]
    _run(qtbot, widget)
    after = viewer.layers["nanog_stardist_segmented"]
    assert after is not before
    assert np.asarray(after.data[0] if after.multiscale else after.data).max() == 1
    assert messages == ["SD: added nanog_stardist_segmented"]


def test_missing_output_is_reported(qtbot, setup):
    viewer, widget, messages, _ = setup(write=False)
    widget._task_ui["SD"]["form"].set_values({"channel": {"identifier": "DAPI"}})
    _run(qtbot, widget)
    assert "DAPI_stardist_segmented" not in viewer.layers
    assert len(messages) == 1
    assert "did not write the expected output" in messages[0]
    assert "'DAPI_stardist_segmented'" in messages[0]


# --------------------------------------------------------------------------
# Label layer names differ between napari-ome-zarr versions:
# 0.6 "DAPI_segmented", 0.10 "labels/labels/DAPI_segmented"
# --------------------------------------------------------------------------


def _fake_reader(label_names):
    def read(path):
        layers = [(np.zeros((2, 8, 8)), {"name": ["DAPI", "nanog"],
                                         "channel_axis": 0}, "image")]
        layers += [(np.ones((8, 8), "uint32"), {"name": n}, "labels")
                   for n in label_names]
        return layers
    return lambda path: read


@pytest.mark.parametrize("prefix", ["", "labels/labels/"])
def test_load_labels_any_reader_naming(qtbot, monkeypatch, prefix):
    monkeypatch.setattr(W, "napari_get_reader", _fake_reader(
        [f"{prefix}nuclei", f"{prefix}DAPI_segmented"]))
    viewer = ViewerModel()
    widget = W.TasksQWidget(viewer)
    qtbot.addWidget(widget)
    added = widget._load_zarr_layers("/d/0", labels={"DAPI_segmented"})
    assert added == ["DAPI_segmented"]
    assert [ly.name for ly in viewer.layers] == ["DAPI_segmented"]


def test_reload_replaces_label_opened_with_path_name(qtbot, monkeypatch):
    # opened with napari-ome-zarr 0.10, then re-run
    monkeypatch.setattr(W, "napari_get_reader",
                        _fake_reader(["labels/labels/DAPI_segmented"]))
    viewer = ViewerModel()
    viewer.add_labels(np.zeros((8, 8), "uint32"),
                      name="labels/labels/DAPI_segmented")
    widget = W.TasksQWidget(viewer)
    qtbot.addWidget(widget)
    widget._load_zarr_layers("/d/0", labels={"DAPI_segmented"})
    assert [ly.name for ly in viewer.layers] == ["DAPI_segmented"]


def test_output_on_disk_but_not_loadable_is_reported(qtbot, setup, monkeypatch):
    viewer, widget, messages, _ = setup()
    widget._task_ui["SD"]["form"].set_values({"channel": {"identifier": "DAPI"}})
    monkeypatch.setattr(W, "napari_get_reader", _fake_reader([]))
    _run(qtbot, widget)
    assert len(messages) == 1
    assert "could not load" in messages[0]
    assert "'DAPI_stardist_segmented'" in messages[0]


# --------------------------------------------------------------------------
# Tasks that rewrite their input image in place (e.g. Apply Registration to
# Image with overwrite_input) report it in image_list_updates
# --------------------------------------------------------------------------

# Replaces the image data with 7s and reports the input image
REWRITE_TASK = '''
import argparse, json, zarr
p = argparse.ArgumentParser(); p.add_argument("--args-json"); p.add_argument("--out-json")
a = p.parse_args(); args = json.load(open(a.args_json))
zarr.open(args["zarr_url"] + "/0", mode="r+")[:] = 7
json.dump({"image_list_updates": [{"zarr_url": args["zarr_url"]}]},
          open(a.out_json, "w"))
'''


def _max(layer):
    return np.asarray(layer.data[0] if layer.multiscale else layer.data).max()


def _open(viewer, url):
    with layer_source(path=url, reader_plugin="napari-ome-zarr"):
        for data in napari_get_reader(url)(url):
            viewer._add_layer_from_data(*data)


def test_rewritten_input_image_is_reloaded(qtbot, setup):
    viewer, widget, messages, tmp_path = setup(task=REWRITE_TASK)
    # A second image with the same channel and label names stays as it is
    other = str(tmp_path / "other.zarr")
    create_ome_zarr_from_array(
        other, np.zeros((2, 32, 32), "uint16"), xy_pixelsize=1.0,
        axes_names="cyx", channel_labels=["DAPI", "nanog"], levels=1,
    ).derive_label("nanog_stardist_segmented").set_array(
        np.zeros((32, 32), "uint32"))
    _open(viewer, other)
    other_layers = [ly for ly in viewer.layers
                    if ly.source.path == other]
    old_dapi = viewer.layers["DAPI"]
    widget._task_ui["SD"]["form"].set_values({"channel": {"identifier": "DAPI"}})

    _run(qtbot, widget)

    new_dapi = viewer.layers["DAPI"]
    assert new_dapi is not old_dapi
    assert _max(new_dapi) == 7
    # the input's open label layer is reloaded too, not dropped
    assert "nanog_stardist_segmented" in viewer.layers
    assert all(ly in viewer.layers for ly in other_layers)
    assert widget._image_layers.currentText() == "DAPI"
    assert messages[0] == "SD: added DAPI, nanog, nanog_stardist_segmented"


def test_label_from_other_image_with_same_name_is_kept(qtbot, setup):
    viewer, widget, messages, tmp_path = setup()
    other = str(tmp_path / "other.zarr")
    create_ome_zarr_from_array(
        other, np.zeros((2, 32, 32), "uint16"), xy_pixelsize=1.0,
        axes_names="cyx", channel_labels=["DAPI", "nanog"], levels=1,
    ).derive_label("DAPI_stardist_segmented").set_array(
        np.zeros((32, 32), "uint32"))
    _open(viewer, other)
    other_label = viewer.layers["DAPI_stardist_segmented"]
    widget._task_ui["SD"]["form"].set_values({"channel": {"identifier": "DAPI"}})

    _run(qtbot, widget)

    assert other_label in viewer.layers
    labels = [ly for ly in viewer.layers
              if W._label_name(ly.name).startswith("DAPI_stardist_segmented")]
    assert len(labels) == 2


def test_reload_keeps_label_renamed_by_napari(qtbot, monkeypatch, tmp_path):
    # napari names a second "org" layer "org [1]"; reloading its image must
    # bring it back under the label's own name
    url = str(tmp_path / "1")
    (tmp_path / "0").mkdir()
    (tmp_path / "1").mkdir()
    monkeypatch.setattr(W, "napari_get_reader", _fake_reader(["org"]))
    viewer = ViewerModel()
    other = viewer.add_labels(np.zeros((8, 8), "uint32"), name="org")
    other.metadata["zarr_url"] = str(tmp_path / "0")
    label = viewer.add_labels(np.zeros((8, 8), "uint32"), name="org")
    label.metadata["zarr_url"] = url
    assert label.name == "org [1]"
    widget = W.TasksQWidget(viewer)
    qtbot.addWidget(widget)
    added = widget._reload_image(url)
    assert "org" in added or "org [1]" in added
    assert label not in viewer.layers and other in viewer.layers
    assert len([ly for ly in viewer.layers if ly.name.startswith("org")]) == 2
