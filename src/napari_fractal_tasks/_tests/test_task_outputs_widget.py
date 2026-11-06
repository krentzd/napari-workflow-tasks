"""After a run, the widget shows the outputs named by the task's arguments."""

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

from napari_fractal_tasks import _show, _task_tab  # noqa: E402
from napari_fractal_tasks._widget import TasksQWidget  # noqa: E402

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


def _open(viewer, url):
    with layer_source(path=url, reader_plugin="napari-ome-zarr"):
        for data in napari_get_reader(url)(url):
            viewer._add_layer_from_data(*data)


@pytest.fixture
def setup(qtbot, tmp_path, monkeypatch, fake_package):
    """``setup(write=True, task=None)``: an image open in the viewer, the
    widget with the tab of task "SD" (``task``: its script) open."""
    def make(write=True, task=None):
        url = str(tmp_path / "img.zarr")
        container = create_ome_zarr_from_array(
            url, np.zeros((2, 32, 32), "uint16"), xy_pixelsize=1.0,
            axes_names="cyx", channel_labels=["DAPI", "nanog"], levels=1,
        )
        # left by an earlier run
        container.derive_label("nanog_stardist_segmented").set_array(
            np.zeros((32, 32), "uint32"))
        package = fake_package(
            [{"name": "SD", "executable_parallel": "task.py",
              "args_schema_parallel": SCHEMA}],
            {"task.py": task or f"WRITE = {write}\n" + TASK})
        messages = []
        for name in ("show_info", "show_warning", "show_error"):
            monkeypatch.setattr(_task_tab, name, messages.append)

        viewer = ViewerModel()
        _open(viewer, url)
        widget = TasksQWidget(viewer)
        qtbot.addWidget(widget)
        widget.add_package(package, "SD")
        tab = widget._tabs["SD [p==1.0]"]
        tab.inputs.combos()[0].choose("DAPI")
        assert tab.inputs.selected() == [url]
        return viewer, widget, tab, messages, tmp_path

    return make


def _run(qtbot, tab):
    tab.run()
    qtbot.waitUntil(lambda: tab._worker is None, timeout=60000)


def test_rerun_shows_label_named_by_arguments(qtbot, setup):
    # Input layer DAPI, but the task segments nanog; its label already exists
    viewer, widget, tab, messages, _ = setup()
    tab.form.set_values({
        "channel": {"identifier": "nanog"},
        "label_name": "{channel_identifier}_stardist_segmented",
    })
    before = viewer.layers["nanog_stardist_segmented"]
    _run(qtbot, tab)
    after = viewer.layers["nanog_stardist_segmented"]
    assert after is not before
    assert np.asarray(after.data[0] if after.multiscale else after.data).max() == 1
    assert messages == ["SD: added nanog_stardist_segmented"]


def test_missing_output_is_reported(qtbot, setup):
    viewer, widget, tab, messages, _ = setup(write=False)
    tab.form.set_values({"channel": {"identifier": "DAPI"}})
    _run(qtbot, tab)
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
    monkeypatch.setattr(_show, "napari_get_reader", _fake_reader(
        [f"{prefix}nuclei", f"{prefix}DAPI_segmented"]))
    viewer = ViewerModel()
    added = _show.add_layers(viewer, "/d/0", labels={"DAPI_segmented"},
                             images=False)
    assert added == ["DAPI_segmented"]
    assert [ly.name for ly in viewer.layers] == ["DAPI_segmented"]


def test_reload_replaces_label_opened_with_path_name(qtbot, monkeypatch):
    # opened with napari-ome-zarr 0.10, then re-run
    monkeypatch.setattr(_show, "napari_get_reader",
                        _fake_reader(["labels/labels/DAPI_segmented"]))
    viewer = ViewerModel()
    viewer.add_labels(np.zeros((8, 8), "uint32"),
                      name="labels/labels/DAPI_segmented")
    _show.add_layers(viewer, "/d/0", labels={"DAPI_segmented"}, images=False)
    assert [ly.name for ly in viewer.layers] == ["DAPI_segmented"]


def test_output_on_disk_but_not_loadable_is_reported(qtbot, setup, monkeypatch):
    viewer, widget, tab, messages, _ = setup()
    tab.form.set_values({"channel": {"identifier": "DAPI"}})
    monkeypatch.setattr(_show, "napari_get_reader", _fake_reader([]))
    _run(qtbot, tab)
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


def test_rewritten_input_image_is_reloaded(qtbot, setup):
    viewer, widget, tab, messages, tmp_path = setup(task=REWRITE_TASK)
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
    tab.form.set_values({"channel": {"identifier": "DAPI"}})

    _run(qtbot, tab)

    new_dapi = viewer.layers["DAPI"]
    assert new_dapi is not old_dapi
    assert _max(new_dapi) == 7
    # the input's open label layer is reloaded too, not dropped
    assert "nanog_stardist_segmented" in viewer.layers
    assert all(ly in viewer.layers for ly in other_layers)
    assert tab.inputs.layers() == ["DAPI"]
    assert messages[0] == "SD: added DAPI, nanog, nanog_stardist_segmented"


def test_label_from_other_image_with_same_name_is_kept(qtbot, setup):
    viewer, widget, tab, messages, tmp_path = setup()
    other = str(tmp_path / "other.zarr")
    create_ome_zarr_from_array(
        other, np.zeros((2, 32, 32), "uint16"), xy_pixelsize=1.0,
        axes_names="cyx", channel_labels=["DAPI", "nanog"], levels=1,
    ).derive_label("DAPI_stardist_segmented").set_array(
        np.zeros((32, 32), "uint32"))
    _open(viewer, other)
    other_label = viewer.layers["DAPI_stardist_segmented"]
    tab.form.set_values({"channel": {"identifier": "DAPI"}})

    _run(qtbot, tab)

    assert other_label in viewer.layers
    labels = [ly for ly in viewer.layers
              if _show.label_name(ly.name).startswith("DAPI_stardist_segmented")]
    assert len(labels) == 2


def test_reload_keeps_label_renamed_by_napari(qtbot, monkeypatch, tmp_path):
    # napari names a second "org" layer "org [1]"; reloading its image must
    # bring it back under the label's own name
    url = str(tmp_path / "1")
    (tmp_path / "0").mkdir()
    (tmp_path / "1").mkdir()
    monkeypatch.setattr(_show, "napari_get_reader", _fake_reader(["org"]))
    viewer = ViewerModel()
    other = viewer.add_labels(np.zeros((8, 8), "uint32"), name="org")
    other.metadata["zarr_url"] = str(tmp_path / "0")
    label = viewer.add_labels(np.zeros((8, 8), "uint32"), name="org")
    label.metadata["zarr_url"] = url
    assert label.name == "org [1]"
    added = _show.reload_image(viewer, url)
    assert "org" in added or "org [1]" in added
    assert label not in viewer.layers and other in viewer.layers
    assert len([ly for ly in viewer.layers if ly.name.startswith("org")]) == 2
