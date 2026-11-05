"""The Main tab lists the installed packages and their tasks; packages are
installed in a separate window, from the catalog or from a .tar.gz."""

import json

import pytest

pytest.importorskip("napari")

from napari.components import ViewerModel  # noqa: E402
from qtpy.QtCore import Qt  # noqa: E402

from workflow_tasks_core import Package, install, paths  # noqa: E402
from workflow_tasks_core._tests.test_local_package import (  # noqa: E402
    MANIFEST,
    PKG_INFO,
    _sdist,
)

from napari_workflow_tasks import _catalog_panel  # noqa: E402
from napari_workflow_tasks._widget import KEY_ROLE, TasksQWidget  # noqa: E402

TASKS = [
    {"name": "Segment", "executable_parallel": "s.py",
     "args_schema_parallel": {"properties": {"zarr_url": {"type": "string"}}}},
    {"name": "Convert", "executable_non_parallel": "c.py",
     "args_schema_non_parallel": {"properties": {"zarr_dir": {"type": "string"}}}},
]


def _rows(widget):
    """{(package, chosen version): [(task, type, can be opened)]}"""
    tree = widget.task_tree
    rows = {}
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        version = widget._version_combos[item.text(0)].currentText()
        rows[item.text(0), version] = [
            (item.child(j).text(0), item.child(j).text(2),
             item.child(j).data(0, KEY_ROLE) is not None)
            for j in range(item.childCount())]
    return rows


def _versions(widget, name):
    combo = widget._version_combos[name]
    return [combo.itemText(i) for i in range(combo.count())]


def _add_version(version):
    """Install another version of the fake package "p" (same tasks)."""
    entry = json.loads((paths.env_dir("p", "1.0") / install.ENTRY_FILE).read_text())
    d = paths.env_dir("p", version)
    (d / ".pixi" / "envs").mkdir(parents=True)
    (d / "pixi.toml").write_text("")
    (d / install.ENTRY_FILE).write_text(json.dumps({**entry, "version": version}))


def test_installed_packages_are_listed(qtbot, fake_package):
    fake_package(TASKS)
    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    assert _rows(widget) == {("p", "1.0"): [
        ("Segment", "parallel", True),
        ("Convert", "converter", False)]}
    assert not widget.open_button.isEnabled()

    package_row = widget.task_tree.topLevelItem(0)
    widget.task_tree.setCurrentItem(package_row.child(1))  # a converter
    assert not widget.open_button.isEnabled()
    widget.task_tree.setCurrentItem(package_row.child(0))
    assert widget.open_button.isEnabled()
    widget.open_button.click()
    assert widget.tabs.currentWidget() is widget._tabs["Segment [p==1.0]"]


def test_reinstalled_package_is_listed_once(qtbot, fake_package):
    package = fake_package(TASKS)
    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    widget.add_package(package, "Segment")
    assert list(_rows(widget)) == [("p", "1.0")]
    assert _versions(widget, "p") == ["1.0"]
    assert len(_rows(widget)["p", "1.0"]) == 2
    assert "Segment [p==1.0]" in widget._tabs


def test_install_window_adds_packages_to_the_list(qtbot, fake_package):
    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    assert _rows(widget) == {}
    widget.show_catalog()
    dialog = widget._catalog_dialog
    assert dialog.isVisible()
    widget.show_catalog()
    assert widget._catalog_dialog is dialog  # one window, kept

    dialog.panel.installed.emit(fake_package(TASKS), "")
    assert list(_rows(widget)) == [("p", "1.0")]
    dialog.close()


def test_install_from_file(qtbot, tmp_path, monkeypatch, fake_package):
    archive = _sdist(tmp_path / "my_tasks-1.0.tar.gz", {
        "PKG-INFO": PKG_INFO,
        "src/my_tasks/__FRACTAL_MANIFEST__.json": json.dumps(MANIFEST)})
    calls = []

    def fake_install(pkg, version, extras=None, log=print, pins=None,
                     use_lock=True, skip=None):
        calls.append((pkg.kind, pkg.pkg_name, version, pins))
        manifest = tmp_path / "installed_manifest.json"
        manifest.write_text(json.dumps(MANIFEST))
        return {"src_path": str(tmp_path), "manifest_path": str(manifest),
                "pixi_manifest_path": "/x/pixi.toml", "version": version,
                "pixi_frozen": False, "env_kind": "resolved"}

    monkeypatch.setattr(install, "install_version", fake_install)
    monkeypatch.setattr(_catalog_panel.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(archive), "")))
    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    widget.show_catalog()
    panel = widget._catalog_dialog.panel
    assert not panel.file_btn.isEnabled()  # no path yet
    panel._browse_file()
    assert panel.file_path.text() == str(archive)
    assert panel.file_btn.isEnabled()
    panel.pins_edit.setText("numpy<2")
    with qtbot.waitSignal(panel.installed, timeout=10000):
        qtbot.mouseClick(panel.file_btn, Qt.LeftButton)
    assert calls == [("local", "my-tasks", "1.0", ["numpy<2"])]
    assert _rows(widget) == {("my-tasks", "1.0"): [("Segment", "parallel", True)]}
    assert panel.status.text().startswith("Installed my-tasks==1.0")
    widget._catalog_dialog.close()


def test_install_from_a_file_that_is_no_package(qtbot, tmp_path, monkeypatch):
    archive = _sdist(tmp_path / "x.tar.gz", {"README": "hi"})
    errors = []
    monkeypatch.setattr(_catalog_panel, "show_error", errors.append)
    panel = _catalog_panel.CatalogPanel()
    qtbot.addWidget(panel)
    panel.file_path.setText(str(archive))
    panel.file_btn.click()
    assert errors and "cannot tell the package's name and version" in errors[0]
    assert panel.file_btn.isEnabled()


def test_version_dropdown(qtbot, fake_package):
    fake_package(TASKS)
    _add_version("0.9")
    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    assert _versions(widget, "p") == ["1.0", "0.9"]  # newest first
    assert list(_rows(widget)) == [("p", "1.0")]
    package_row = widget.task_tree.topLevelItem(0)
    assert package_row.child(0).data(0, KEY_ROLE) == "Segment [p==1.0]"

    widget._version_combos["p"].setCurrentText("0.9")
    assert package_row.child(0).data(0, KEY_ROLE) == "Segment [p==0.9]"
    widget.task_tree.setCurrentItem(package_row.child(0))
    widget.open_button.click()
    assert "Segment [p==0.9]" in widget._tabs

    # a newly added version is shown
    _add_version("1.1")
    widget.add_package(Package.installed("p", "1.1"))
    assert _versions(widget, "p") == ["1.1", "1.0", "0.9"]
    assert list(_rows(widget)) == [("p", "1.1")]


def _installed(panel):
    items = [panel.installed_list.item(i) for i in range(panel.installed_list.count())]
    return [item.text() for item in items]


@pytest.fixture
def yes(monkeypatch):
    """Answer "Yes" to the "Remove package?" question."""
    monkeypatch.setattr(_catalog_panel.QMessageBox, "question",
                        staticmethod(lambda *a, **k: _catalog_panel.QMessageBox.Yes))


def test_remove_package(qtbot, fake_package, yes):
    fake_package(TASKS)
    _add_version("0.9")
    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    widget.open_task("Segment [p==1.0]")
    widget.show_catalog()
    panel = widget._catalog_dialog.panel
    assert panel.status.text() == ""
    assert _installed(panel) == ["p 0.9", "p 1.0"]
    assert not panel.remove_btn.isEnabled()  # nothing selected

    panel.installed_list.setCurrentRow(1)  # p 1.0
    with qtbot.waitSignal(panel.removed, timeout=10000):
        panel.remove_btn.click()
    assert not paths.env_dir("p", "1.0").exists()
    assert _installed(panel) == ["p 0.9"]
    assert panel.status.text() == "Removed p 1.0."
    # its tab is closed; the other version is still listed
    assert "Segment [p==1.0]" not in widget._tabs
    assert _versions(widget, "p") == ["0.9"]
    assert list(_rows(widget)) == [("p", "0.9")]

    panel.installed_list.setCurrentRow(0)
    with qtbot.waitSignal(panel.removed, timeout=10000):
        panel.remove_btn.click()
    assert _rows(widget) == {}
    assert _installed(panel) == ["(no packages installed yet)"]
    widget._catalog_dialog.close()


def test_no_removal_while_a_task_runs(qtbot, fake_package):
    fake_package(TASKS)
    widget = TasksQWidget(ViewerModel())
    qtbot.addWidget(widget)
    widget.show_catalog()
    panel = widget._catalog_dialog.panel
    panel.installed_list.setCurrentRow(0)
    assert panel.remove_btn.isEnabled()
    widget._set_running(True)
    assert not panel.remove_btn.isEnabled()
    assert "while a task runs" in panel.remove_btn.toolTip()
    widget._set_running(False)
    assert panel.remove_btn.isEnabled()
    widget._catalog_dialog.close()
