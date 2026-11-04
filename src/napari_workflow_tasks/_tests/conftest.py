import pytest

from napari_workflow_tasks import catalog


@pytest.fixture(autouse=True)
def _tasks_dir(tmp_path, monkeypatch):
    """Task run files never go to the real ~/.napari_workflow_tasks."""
    monkeypatch.setattr(catalog, "TASKS_DIR", tmp_path / "tasks")
