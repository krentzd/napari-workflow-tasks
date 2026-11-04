"""
Core of napari-workflow-tasks: browse Fractal task packages, install each
into its own pixi environment and run tasks on OME-Zarr images.

Everything here works headless. The napari plugin
(``napari_workflow_tasks``) only adds the widgets and viewer handling on
top of it.

Rule: nothing in this package may import napari, Qt (qtpy, PyQt5/6,
PySide2/6, superqt), magicgui or napari_ome_zarr, not even indirectly
(see ``_tests/test_no_napari.py``).
"""

from .api import Catalog, Package, Task
from .runner import TaskFailed
from .schema import SchemaError

__all__ = ["Catalog", "Package", "Task", "SchemaError", "TaskFailed"]
