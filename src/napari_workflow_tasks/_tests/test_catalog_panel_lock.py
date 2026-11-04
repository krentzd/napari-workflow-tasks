"""Headless CatalogPanel: the "Use the package's pixi.lock" checkbox greys
out extras (not pins: pinned versions win) for locked releases and reaches
install_version."""
import pytest

from napari_workflow_tasks import catalog

pytest.importorskip("napari")


def _pkg(lock):
    versions = [
        catalog.PackageVersion("1.1", "https://x/my_tasks-1.1-py3-none-any.whl",
                               "my_tasks-1.1-py3-none-any.whl", pixi_lock=lock),
        catalog.PackageVersion("1.0", "https://x/my_tasks-1.0-py3-none-any.whl",
                               "my_tasks-1.0-py3-none-any.whl", pixi_lock=False),
    ]
    return catalog.TaskPackage("https://pypi.org/project/cellpose-like", "pypi",
                               "fractal-cellpose-sam-task", versions)


@pytest.fixture
def panel(qtbot, tmp_path, monkeypatch):
    from napari_workflow_tasks._catalog_panel import CatalogPanel

    monkeypatch.setattr(catalog, "ENVS_DIR", tmp_path / "envs")
    monkeypatch.setattr(catalog, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    w = CatalogPanel()
    qtbot.addWidget(w)
    pkg = _pkg(True)
    w._packages[pkg.pkg_name] = pkg
    info = catalog.VersionInfo({"task_list": [
        {"name": "T", "executable_parallel": "t.py"}]}, "my_tasks",
        "my_tasks/__FRACTAL_MANIFEST__.json", [], ">=3.11")
    w._infos[(pkg.pkg_name, "1.1")] = info
    w._infos[(pkg.pkg_name, "1.0")] = info
    w._add_package_row(pkg)
    w.tree.setCurrentItem(w._items[pkg.pkg_name])
    return w, pkg


def test_lock_hint(panel):
    w, pkg = panel
    assert "🔒" in w._combos[pkg.pkg_name].itemText(0)
    assert w.lock_check.isChecked()
    # locked release: extras greyed out; pins always used (pre-filled with
    # the suggested pins), they decide whether the lock is used as is
    assert not w.extras_edit.isEnabled()
    assert "falls back" in w.extras_edit.toolTip()
    assert w.pins_edit.isEnabled()
    assert w.pins_edit.text() == "cellpose<4.2"
    assert "satisfies the pins" in w.pins_edit.toolTip()
    assert "always win" in w.pins_edit.toolTip()
    tip = w.lock_check.toolTip()
    assert "pinned packages are not used" not in tip
    assert "pip extras are not used" in tip and "Pinned packages win" in tip
    w.lock_check.setChecked(False)
    assert w.pins_edit.isEnabled() and w.extras_edit.isEnabled()
    assert "satisfies the pins" not in w.pins_edit.toolTip()
    w.lock_check.setChecked(True)
    assert w.pins_edit.isEnabled() and not w.extras_edit.isEnabled()
    # a version without a lock: everything editable
    w._combos[pkg.pkg_name].setCurrentIndex(1)
    assert w.pins_edit.isEnabled() and w.extras_edit.isEnabled()
    assert w.extras_edit.toolTip() == "Available extras: none"
    assert "satisfies the pins" not in w.pins_edit.toolTip()


@pytest.mark.parametrize("checked", [True, False])
def test_checkbox_reaches_install_version(panel, qtbot, monkeypatch, checked):
    w, pkg = panel
    calls = []

    def fake_install(pkg_, version, extras=None, log=print, pins=None, use_lock=True):
        calls.append((version, extras, pins, use_lock))
        return {"src_path": "/x", "manifest_path": "/x/m.json",
                "pixi_manifest_path": "/x/pixi.toml", "version": version,
                "pixi_frozen": use_lock, "env_kind": "pixi.lock" if use_lock else "resolved",
                "pins_applied": {"pins": pins, "how": "x"}}

    monkeypatch.setattr(catalog, "install_version", fake_install)
    monkeypatch.setattr(catalog, "get_version_info", lambda p, v: w._infos[(p.pkg_name, v)])
    w.lock_check.setChecked(checked)
    with qtbot.waitSignal(w.installed, timeout=10000):
        w._install_selected()
    assert calls == [("1.1", [], ["cellpose<4.2"], checked)]
    assert ("from its pixi.lock" in w.status.text()) == checked
    assert "pinned: cellpose<4.2" in w.status.text()
