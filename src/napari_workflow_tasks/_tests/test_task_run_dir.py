"""Run files go to tasks/<package>/<version>/<task>/ under the plugin root."""

import pytest

from napari_workflow_tasks import catalog


def test_task_run_dir_layout(tmp_path):
    d = catalog.task_run_dir("fractal-cellpose-sam-task", "0.2.3",
                             "Cellpose SAM Segmentation")
    assert d == (tmp_path / "tasks" / "fractal-cellpose-sam-task" / "0.2.3"
                 / "Cellpose SAM Segmentation")


def test_task_run_dir_safe_names(tmp_path):
    d = catalog.task_run_dir("pkg", "1.0", 'Copy a/b: "x"?')
    assert d.parent == tmp_path / "tasks" / "pkg" / "1.0"
    assert d.name == "Copy a_b_ _x__"


def test_task_run_dir_follows_root(tmp_path, monkeypatch):
    # TASKS_DIR is read on each call, so a changed root is picked up
    monkeypatch.setattr(catalog, "TASKS_DIR", tmp_path / "elsewhere")
    assert catalog.task_run_dir("p", "1", "t").parent.parent.parent == (
        tmp_path / "elsewhere")


# --------------------------------------------------------------------------
# FractalTaskManager: where a task's run files go
# --------------------------------------------------------------------------


def test_task_manager_paths(tmp_path):
    pytest.importorskip("napari")
    from napari_workflow_tasks._widget import FractalTaskManager

    tm = FractalTaskManager()
    tm.add_task(
        name="Cellpose SAM Segmentation [fractal-cellpose-sam-task==0.2.3]",
        base_name="Cellpose SAM Segmentation", version="0.2.3",
        package_name="fractal-cellpose-sam-task==0.2.3",
        parent_dir="/env/src", executable_parallel="task.py",
        properties={}, defs={}, required=[], type="object",
        title="CellposeSamSegmentationTask",
    )
    key = "Cellpose SAM Segmentation [fractal-cellpose-sam-task==0.2.3]"
    run_dir = (tmp_path / "tasks" / "fractal-cellpose-sam-task" / "0.2.3"
               / "Cellpose SAM Segmentation")
    assert tm.get_run_dir(key) == run_dir
    assert tm.get_path_to_json(key) == str(run_dir / "CellposeSamSegmentationTask.json")
    assert tm.get_out_json_path(key) == str(run_dir / "out_file.json")
    # the executable still runs from the installed package
    assert tm.get_executable_path(key) == "/env/src/task.py"
