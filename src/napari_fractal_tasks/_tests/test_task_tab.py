"""Task tabs: their input dropdowns, their ROI table, one task running at a
time."""

import json
import sys

import numpy as np
import pytest

pytest.importorskip("napari")
if sys.platform == "win32":
    pytest.skip("uses a POSIX shell script as fake pixi", allow_module_level=True)

from napari.components import ViewerModel  # noqa: E402
from ngio import create_ome_zarr_from_array  # noqa: E402

from workflow_tasks_core.zarr_meta import list_tables  # noqa: E402

from napari_fractal_tasks import _task_tab  # noqa: E402
from napari_fractal_tasks._widget import TasksQWidget  # noqa: E402

PARALLEL = {"properties": {"zarr_url": {"type": "string"},
                           "input_ROI_table": {"type": "string",
                                               "default": "FOV_ROI_table"}},
            "required": ["zarr_url"]}
NON_PARALLEL = {"properties": {"zarr_urls": {"type": "array"},
                               "zarr_dir": {"type": "string"}},
                "required": ["zarr_urls", "zarr_dir"]}
# Waits until the file "go" exists next to the image
WAIT = '''
import argparse, json, os, time
p = argparse.ArgumentParser(); p.add_argument("--args-json"); p.add_argument("--out-json")
a = p.parse_args(); args = json.load(open(a.args_json))
go = os.path.join(os.path.dirname(args["zarr_url"]), "go")
while not os.path.exists(go):
    time.sleep(0.05)
json.dump(None, open(a.out_json, "w"))
'''


def _write_attrs(path, attrs):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".zattrs").write_text(json.dumps(attrs))
    return str(path)


def _image(path):
    return _write_attrs(path, {"multiscales": [{"datasets": [{"path": "0"}]}]})


@pytest.fixture
def messages(monkeypatch):
    shown = []
    for name in ("show_info", "show_warning", "show_error"):
        monkeypatch.setattr(_task_tab, name, shown.append)
    return shown


@pytest.fixture
def widget(qtbot, tmp_path, fake_package, messages):
    """Two images with layers of their own, and a plate layer."""
    plate = tmp_path / "plate.zarr"
    _write_attrs(plate, {"plate": {"wells": [{"path": "B/03"}]}})
    _write_attrs(plate / "B" / "03", {"well": {"images": [{"path": "0"},
                                                          {"path": "1"}]}})
    images = [_image(plate / "B" / "03" / "0"), _image(plate / "B" / "03" / "1")]

    viewer = ViewerModel()
    viewer.add_image(np.zeros((4, 4)), name="plate",
                     metadata={"zarr_url": str(plate)})
    for i, url in enumerate(images):
        viewer.add_image(np.zeros((4, 4)), name=f"image {i}",
                         metadata={"zarr_url": url})
    w = TasksQWidget(viewer)
    qtbot.addWidget(w)
    w.add_package(fake_package(
        [{"name": "Wait", "executable_parallel": "wait.py",
          "args_schema_parallel": PARALLEL},
         {"name": "All", "executable_non_parallel": "all.py",
          "args_schema_non_parallel": NON_PARALLEL}],
        {"wait.py": WAIT}))
    w.open_task("Wait [p==1.0]")
    w.open_task("All [p==1.0]")
    w.viewer, w.images = viewer, images
    return w


def _items(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def test_only_image_layers_of_images_are_listed(widget):
    (combo,) = widget._tabs["Wait [p==1.0]"].inputs.combos()
    assert _items(combo) == ["image 0", "image 1"]  # not the plate layer


def test_parallel_task_takes_one_image(widget):
    tab = widget._tabs["Wait [p==1.0]"]
    (combo,) = tab.inputs.combos()
    assert tab.inputs.selected() == [widget.images[0]]
    combo.choose("image 1")
    assert tab.inputs.selected() == [widget.images[1]]
    assert "+ Add image" not in [b.text() for b in tab.findChildren(
        _task_tab.QPushButton)]


def test_non_parallel_task_takes_several_images(widget):
    tab = widget._tabs["All [p==1.0]"]
    first, second = widget.images
    tab.inputs.add_row()  # shows an image that is not chosen yet
    assert tab.inputs.layers() == ["image 0", "image 1"]
    assert tab.inputs.selected() == [first, second]
    assert tab.zarr_dir.text() == first.rsplit("/plate.zarr", 1)[0]
    tab.inputs.combos()[1].choose("image 0")
    assert tab.inputs.selected() == [first]  # each image once

    row, _combo, remove_button = tab.inputs._rows[1]
    remove_button.click()
    assert len(tab.inputs.combos()) == 1
    assert not tab.inputs._rows[0][2].isEnabled()  # the last one stays


def test_input_survives_reloading_its_layer(widget):
    """A task that rewrote its input reloads it: the layer is removed and
    added again under the same name."""
    tab = widget._tabs["Wait [p==1.0]"]
    tab.inputs.combos()[0].choose("image 1")
    widget.viewer.layers.remove("image 1")
    assert tab.inputs.layers() == ["image 0"]
    widget.viewer.add_image(np.zeros((4, 4)), name="image 1",
                            metadata={"zarr_url": widget.images[1]})
    assert tab.inputs.layers() == ["image 1"]


def test_one_task_runs_at_a_time(widget, qtbot):
    wait, other = widget._tabs["Wait [p==1.0]"], widget._tabs["All [p==1.0]"]
    assert other.run_button.isEnabled()
    wait.run()
    assert wait._worker is not None
    assert not wait.run_button.isEnabled()
    assert not other.run_button.isEnabled()
    assert not wait.remove_button.isEnabled()
    # a tab opened while a task runs is blocked too
    widget.close_task("All [p==1.0]")
    widget.open_task("All [p==1.0]")
    other = widget._tabs["All [p==1.0]"]
    assert not other.run_button.isEnabled()

    with open(widget.images[0].rsplit("/", 1)[0] + "/go", "w"):
        pass
    qtbot.waitUntil(lambda: wait._worker is None, timeout=30000)
    assert wait.status.text().startswith("✓")
    assert wait.run_button.isEnabled() and other.run_button.isEnabled()


def test_roi_table_is_saved_and_used(qtbot, tmp_path, fake_package, messages):
    url = str(tmp_path / "img.zarr")
    create_ome_zarr_from_array(url, np.zeros((32, 32), "uint16"),
                               xy_pixelsize=1.0, axes_names="yx", levels=1)
    viewer = ViewerModel()
    viewer.add_image(np.zeros((32, 32)), name="img", metadata={"zarr_url": url})
    widget = TasksQWidget(viewer)
    qtbot.addWidget(widget)
    widget.add_package(fake_package([{
        "name": "Seg", "executable_parallel": "seg.py",
        "args_schema_parallel": PARALLEL}]), "Seg")
    tab = widget._tabs["Seg [p==1.0]"]

    tab.save_rois()
    assert messages[-1] == "Draw the ROIs in a Shapes layer first."

    tab._add_shapes_layer()
    viewer.layers["ROI selection"].add_rectangles(np.array([[2, 2], [10, 12]]))
    tab.roi_table_name.setText("my_rois")
    tab.save_rois()
    assert "my_rois" in list_tables(url)
    assert tab.form.get_args()["input_ROI_table"] == "my_rois"
    assert "set input_ROI_table" in tab.status.text()

    tab.save_rois()  # exists already, not overwritten
    assert messages[-1].startswith("Could not save the ROI table")
    tab.roi_overwrite.setChecked(True)
    tab.save_rois()
    assert "Saved 1 ROI(s)" in tab.status.text()


def test_roi_table_fills_iterate_over_rois(qtbot, tmp_path, fake_package,
                                           messages):
    """Cellpose SAM: iterator_configuration ("Iterator Configuration", off
    by default) holds roi_table ("Iterate Over ROIs")."""
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).parent / "data" / "cellpose_sam_0.2.3_args_schema.json")
        .read_text())["args_schema_parallel"]
    url = str(tmp_path / "img.zarr")
    create_ome_zarr_from_array(url, np.zeros((32, 32), "uint16"),
                               xy_pixelsize=1.0, axes_names="yx", levels=1)
    viewer = ViewerModel()
    viewer.add_image(np.zeros((32, 32)), name="img", metadata={"zarr_url": url})
    widget = TasksQWidget(viewer)
    qtbot.addWidget(widget)
    widget.add_package(fake_package([{
        "name": "SAM", "executable_parallel": "sam.py",
        "args_schema_parallel": schema}]), "SAM")
    tab = widget._tabs["SAM [p==1.0]"]
    assert "iterator_configuration" not in tab.form.body.values(omit_defaults=True)

    tab._add_shapes_layer()
    viewer.layers["ROI selection"].add_rectangles(np.array([[2, 2], [10, 12]]))
    tab.roi_table_name.setText("my_rois")
    tab.save_rois()
    args = tab.form.body.values(omit_defaults=True)
    assert args["iterator_configuration"]["roi_table"] == "my_rois"
    assert "set iterator_configuration.roi_table" in tab.status.text()


def test_full_description(qtbot, fake_package):
    """"Show more" continues the summary in the same label and style."""
    docs = ("### Purpose\n- **Applies** the registration to the images of "
            "a plate. " + "More about it. " * 20 + "\n- Masks regions.\n\n"
            "### Limitations\n- Only HCS plates.")
    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    widget.add_package(fake_package([
        {"name": "Long", "executable_parallel": "l.py", "docs_info": docs,
         "args_schema_parallel": PARALLEL},
        {"name": "Short", "executable_parallel": "s.py",
         "docs_info": "Segments nuclei.", "args_schema_parallel": PARALLEL},
    ]))
    widget.open_task("Long [p==1.0]")
    tab = widget._tabs["Long [p==1.0]"]
    full = ("Applies the registration to the images of a plate. "
            + "More about it. " * 20 + "Masks regions.\n\n"
            "Limitations: Only HCS plates.")
    short = tab.docs.text()
    assert short.endswith("…") and len(short) <= 301
    assert full.startswith(short[:-1])  # the summary is how it starts
    tab.docs_toggle.click()
    assert tab.docs.text() == full
    assert tab.docs_toggle.text() == "Show less"
    tab.docs_toggle.click()
    assert tab.docs.text() == short

    widget.open_task("Short [p==1.0]")
    short = widget._tabs["Short [p==1.0]"]
    assert short.docs.text() == "Segments nuclei."
    assert short.docs_toggle is None  # nothing more to show
