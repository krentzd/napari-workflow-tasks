"""ROIs from polygons, saved as an ngio ROI table."""

import numpy as np
from ngio import create_ome_zarr_from_array, open_ome_zarr_container

from workflow_tasks_core.rois import create_roi_from_bbox, write_roi_table


def test_create_roi_from_bbox():
    roi = create_roi_from_bbox(np.array([[2, 3], [10, 3], [10, 8], [2, 8]]),
                               roi_id=4)
    assert roi.name == "FOV_4"
    assert (roi.x, roi.y, roi.x_length, roi.y_length) == (3, 2, 5, 8)


def test_write_roi_table(tmp_path):
    url = str(tmp_path / "img.zarr")
    create_ome_zarr_from_array(url, np.zeros((32, 32), "uint16"),
                               xy_pixelsize=1.0, axes_names="yx", levels=1)
    polygons = [np.array([[0, 0], [4, 4]]), [[5, 5], [9, 7], [6, 9]]]

    rois = write_roi_table(url, polygons, "my_rois")
    assert [r.name for r in rois] == ["FOV_1", "FOV_2"]

    table = open_ome_zarr_container(url).get_roi_table("my_rois")
    assert sorted(r.name for r in table.rois()) == ["FOV_1", "FOV_2"]
