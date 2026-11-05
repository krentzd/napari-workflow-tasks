"""Run files go to tasks/<package>/<version>/<task>/ under paths.ROOT_DIR."""

from workflow_tasks_core import paths


def test_task_run_dir_layout(tmp_path):
    d = paths.task_run_dir("fractal-cellpose-sam-task", "0.2.3",
                           "Cellpose SAM Segmentation")
    assert d == (tmp_path / "tasks" / "fractal-cellpose-sam-task" / "0.2.3"
                 / "Cellpose SAM Segmentation")


def test_task_run_dir_safe_names(tmp_path):
    d = paths.task_run_dir("pkg", "1.0", 'Copy a/b: "x"?')
    assert d.parent == tmp_path / "tasks" / "pkg" / "1.0"
    assert d.name == "Copy a_b_ _x__"


def test_task_run_dir_follows_root(tmp_path, monkeypatch):
    # TASKS_DIR is read on each call, so a changed root is picked up
    monkeypatch.setattr(paths, "TASKS_DIR", tmp_path / "elsewhere")
    assert paths.task_run_dir("p", "1", "t").parent.parent.parent == (
        tmp_path / "elsewhere")
