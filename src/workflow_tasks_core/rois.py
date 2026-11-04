"""
Regions of interest (ROIs) for ngio ROI tables, e.g. from a box drawn on
an image.

No napari here: the plugin passes the polygons drawn in a Shapes layer.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from ngio import Roi, open_ome_zarr_container
from ngio.tables import RoiTable

def create_roi_from_bbox(data: np.ndarray, roi_id: Optional[int]= None):
    """Create roi_table roi from 2D bounding box coordinates.

    Parameters
    ----------
    data : (N, 2) array
        Points around which the box is created.
    roi_id : int, optional
        ID to assign to the roi name.

    Returns
    -------
    roi_crop : Roi
    """
    top_left = np.min(data, axis=(0))
    bottom_right = np.max(data, axis=(0))
    
    y, x = top_left[0], top_left[1]
    x_length = bottom_right[1] - top_left[1]
    y_length = bottom_right[0] - top_left[0]

    if roi_id is not None:
        name = f"FOV_{roi_id}"
    else:
        name = "FOV_1"
    
    roi_crop = Roi(y=y, x=x, x_length=x_length, y_length=y_length, name=name)
    return roi_crop


def write_roi_table(
    zarr_url: str,
    polygons: list,
    table_name: str,
    overwrite: bool = False,
) -> list[Roi]:
    """Save one ROI per polygon (its bounding box) as an ROI table of the
    OME-Zarr image at ``zarr_url``. The ROIs are named ``FOV_1``,
    ``FOV_2``, ...

    Parameters
    ----------
    zarr_url : str
        The OME-Zarr image.
    polygons : list of (N, 2) arrays
        Polygon vertices, e.g. the data of a napari Shapes layer.
    table_name : str
        Name of the ROI table.
    overwrite : bool
        Replace an existing table with that name.

    Returns
    -------
    rois : list of Roi
    """
    rois = []
    for roi_id, polygon_array in enumerate(polygons):
        polygon_array = np.array(polygon_array)
        rois.append(create_roi_from_bbox(polygon_array, roi_id=roi_id+1))

    ome_zarr = open_ome_zarr_container(zarr_url)
    ome_zarr.add_table(table_name, RoiTable(rois=rois), overwrite=overwrite)
    return rois
