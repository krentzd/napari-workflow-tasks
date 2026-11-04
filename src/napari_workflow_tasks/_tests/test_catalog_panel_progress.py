"""CatalogPanel progress bar: shown while a package is installed, labelled
with the current step and the elapsed time, hidden when the install ends."""
import pytest

from workflow_tasks_core import paths

pytest.importorskip("napari")


@pytest.fixture
def panel(qtbot, tmp_path, monkeypatch):
    from napari_workflow_tasks._catalog_panel import CatalogPanel

    monkeypatch.setattr(paths, "ENVS_DIR", tmp_path / "envs")
    monkeypatch.setattr(paths, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    w = CatalogPanel()
    qtbot.addWidget(w)
    return w


def test_progress_hidden_initially(panel):
    assert panel.progress.isHidden()
    assert panel.detail.isHidden()


def test_progress_follows_install_log(panel):
    panel._install_label = "my-tasks==1.1"
    panel._start_busy("Preparing my-tasks==1.1")
    panel.detail.setVisible(True)
    assert not panel.progress.isHidden()
    assert panel.status.text().startswith("Preparing my-tasks==1.1… (")

    panel._on_install_log("Solving my-tasks==1.1 from its pixi config")
    assert panel.status.text().startswith(
        "Installing dependencies (this can take several minutes): "
        "my-tasks==1.1… (")

    # pixi output only updates the detail line, not the step
    panel._on_install_log("  ✔ The default environment has been installed.")
    assert "Installing dependencies" in panel.status.text()
    assert panel.detail.text() == "✔ The default environment has been installed."

    panel._on_install_log("Reading the task list")
    assert panel.status.text().startswith("Reading the task list: my-tasks==1.1")

    panel._stop_busy()
    assert panel.progress.isHidden()
    assert panel.detail.isHidden()
    # The timer no longer rewrites the status once stopped
    panel.status.setText("Installed my-tasks==1.1.")
    panel._show_step()
    assert panel.status.text() == "Installed my-tasks==1.1."


def test_progress_shown_while_catalog_loads(panel, monkeypatch):
    started = []
    monkeypatch.setattr(panel, "_start", lambda worker: started.append(worker))
    panel.refresh()
    assert not panel.progress.isHidden()
    assert panel.status.text().startswith("Fetching task packages… (")
    panel._on_catalog_done()
    assert panel.progress.isHidden()
    assert started
