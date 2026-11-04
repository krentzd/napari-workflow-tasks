import pytest

from workflow_tasks_core import paths


@pytest.fixture(autouse=True)
def _tasks_dir(tmp_path, monkeypatch):
    """Task run files never go to the real ~/.napari_workflow_tasks."""
    monkeypatch.setattr(paths, "TASKS_DIR", tmp_path / "tasks")
