"""Task.show(): the inputs with only this run's labels, plus new images."""

import sys

import pytest

pytest.importorskip("napari")
if sys.platform == "win32":
    pytest.skip("uses a POSIX shell script as fake pixi", allow_module_level=True)

from napari.components import ViewerModel  # noqa: E402

from workflow_tasks_core import Package  # noqa: E402
from workflow_tasks_core._tests.test_api import _make_image, env  # noqa: E402, F401


def test_show_one_image(env):  # noqa: F811
    pkg = Package.installed("fake-tasks")
    seg = pkg.task("Segment")
    seg.run(env["url"], channels={"identifiers": ["nanog"]}, log=None)
    seg.run(env["url"], channels={"identifiers": ["DAPI"]}, log=None)
    viewer = seg.show(ViewerModel())
    # only the label of the latest run, not nanog_segmented
    assert [(ly.name, type(ly).__name__) for ly in viewer.layers] == [
        ("DAPI", "Image"), ("nanog", "Image"), ("DAPI_segmented", "Labels")]
    assert viewer.layers["DAPI_segmented"].metadata["zarr_url"] == env["url"]


def test_show_new_images_with_suffix(env):  # noqa: F811
    project = Package.installed("fake-tasks").task("Project")
    project.run(env["url"], log=None)
    viewer = project.show(ViewerModel())
    assert [ly.name for ly in viewer.layers] == [
        "DAPI [plate.zarr/B/03/0]", "nanog [plate.zarr/B/03/0]",
        "DAPI [plate.zarr/B/03/0_mip]", "nanog [plate.zarr/B/03/0_mip]"]


def test_show_before_run(env):  # noqa: F811
    with pytest.raises(RuntimeError, match="run the task first"):
        Package.installed("fake-tasks").task("Segment").show(ViewerModel())
