"""Import / export of task arguments from a task tab."""
import json

import pytest

pytest.importorskip("napari")

from napari.components import ViewerModel  # noqa: E402

from napari_fractal_tasks import _task_tab  # noqa: E402
from napari_fractal_tasks._widget import TasksQWidget  # noqa: E402
from .test_task_outputs_widget import SCHEMA  # noqa: E402

COMPOUND_NP = {
    "properties": {"zarr_urls": {"type": "array"}, "zarr_dir": {"type": "string"},
                   "reference_acquisition": {"type": "integer", "default": 0}},
    "required": ["zarr_urls", "zarr_dir"],
}
COMPOUND_P = {
    "properties": {"zarr_url": {"type": "string"}, "init_args": {"type": "object"},
                   "wavelength_id": {"type": "string"},
                   "level": {"type": "integer", "default": 2}},
    "required": ["zarr_url", "init_args", "wavelength_id"],
}


@pytest.fixture
def widget(qtbot, monkeypatch, fake_package):
    messages = []
    for name in ("show_info", "show_warning", "show_error"):
        monkeypatch.setattr(_task_tab, name, messages.append)
    w = TasksQWidget(ViewerModel())
    qtbot.addWidget(w)
    package = fake_package([
        {"name": "SD", "executable_parallel": "t.py",
         "args_schema_parallel": SCHEMA},
        {"name": "Reg", "executable_parallel": "c.py",
         "executable_non_parallel": "i.py",
         "args_schema_parallel": COMPOUND_P,
         "args_schema_non_parallel": COMPOUND_NP},
    ])
    w.add_package(package)
    for key in ("SD [p==1.0]", "Reg [p==1.0]"):
        w.open_task(key)
    w.messages = messages
    return w


def _save_to(monkeypatch, path):
    monkeypatch.setattr(_task_tab.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))


def test_export_and_import_parallel(widget, tmp_path, monkeypatch):
    tab = widget._tabs["SD [p==1.0]"]
    form = tab.form
    form.set_values({"channel": {"identifier": "nanog"}, "label_name": "nuc"})
    path = tmp_path / "args-sd.json"
    _save_to(monkeypatch, path)
    tab.export_args()
    data = json.loads(path.read_text())
    assert data == {"args_parallel": {"channel": {"identifier": "nanog"},
                                      "label_name": "nuc"},
                    "args_non_parallel": None}

    form.reset()
    tab.import_args(path=str(path))
    assert form.get_args() == {"channel": {"identifier": "nanog"}, "label_name": "nuc"}


def test_compound_workflow_export_and_import(widget, tmp_path, monkeypatch):
    tab = widget._tabs["Reg [p==1.0]"]
    tab.form_np.set_values({"reference_acquisition": 1})
    tab.form.set_values({"wavelength_id": "A01_C01", "level": 0})
    path = tmp_path / "wf.json"
    _save_to(monkeypatch, path)
    tab.export_args(workflow=True)
    entry = json.loads(path.read_text())["task_list"][0]
    assert entry["task"] == {"pkg_name": "p", "version": "1.0", "name": "Reg"}
    assert entry["args_non_parallel"] == {"reference_acquisition": 1}
    assert entry["args_parallel"] == {"wavelength_id": "A01_C01", "level": 0}

    tab.reset()
    tab.import_args(path=str(path))
    assert tab.form_np.get_args() == {"reference_acquisition": 1}
    assert tab.form.get_args() == {"wavelength_id": "A01_C01", "level": 0}


def test_import_reports_problems(widget, tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"args_parallel": {
        "channel": {"identifier": "DAPI"}, "nope": 1, "zarr_url": "/x"}}))
    tab = widget._tabs["SD [p==1.0]"]
    tab.import_args(path=str(path))
    status = tab.status.text()
    assert "unknown arguments ignored: nope" in status
    assert "zarr_url" in status

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "wf", "task_list": [
        {"task": {"name": "Other", "pkg_name": "p"}, "args_parallel": {}}]}))
    tab.import_args(path=str(bad))
    assert "no task 'SD'" in widget.messages[-1]


def test_export_buttons(widget, tmp_path, monkeypatch, qtbot):
    from qtpy.QtCore import Qt
    from qtpy.QtWidgets import QPushButton

    tab = widget._tabs["SD [p==1.0]"]
    buttons = {b.text(): b for b in tab.findChildren(QPushButton)}
    assert "Export as workflow…" not in buttons
    path = tmp_path / "args.json"
    _save_to(monkeypatch, path)
    qtbot.mouseClick(buttons["Export"], Qt.LeftButton)
    assert "args_parallel" in json.loads(path.read_text())

    monkeypatch.setattr(_task_tab.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    qtbot.mouseClick(buttons["Import"], Qt.LeftButton)
    assert "Imported" in tab.status.text()
