"""workflow_tasks_core must work without napari or Qt installed."""

import subprocess
import sys

BLOCKED = [
    "napari",
    "qtpy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "superqt",
    "magicgui",
    "napari_ome_zarr",
]

# Runs in a fresh interpreter. A None entry in sys.modules makes any import
# of that name raise ImportError, as if it were not installed.
SCRIPT = """
import importlib
import pkgutil
import sys

for name in {blocked!r}:
    sys.modules[name] = None

import workflow_tasks_core

for module in pkgutil.walk_packages(
    workflow_tasks_core.__path__, "workflow_tasks_core."
):
    if "._tests" in module.name:
        continue
    importlib.import_module(module.name)
    print(module.name)
"""


def test_core_imports_without_napari():
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT.format(blocked=BLOCKED)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    imported = result.stdout.split()
    assert "workflow_tasks_core.paths" in imported
    assert "workflow_tasks_core.task_packages" in imported
    assert "workflow_tasks_core.manifests" in imported
    assert "workflow_tasks_core.snapshot" in imported
    assert "workflow_tasks_core.pixi_config" in imported
    assert "workflow_tasks_core.install" in imported
    assert "workflow_tasks_core.runner" in imported
    assert "workflow_tasks_core.schema" in imported
