"""Import / export of task arguments from a task tab."""
import json

import pytest

pytest.importorskip("napari")

from napari.components import ViewerModel  # noqa: E402

from napari_workflow_tasks import _widget as W  # noqa: E402
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
def widget(qtbot, tmp_path, monkeypatch):
    messages = []
    for name in ("show_info", "show_warning", "show_error"):
        monkeypatch.setattr(W, name, messages.append)
    w = W.TasksQWidget(ViewerModel())
    qtbot.addWidget(w)
    w.task_manager.add_task(
        name="SD [p==1.0]", base_name="SD", package_name="p==1.0", version="1.0",
        parent_dir=str(tmp_path), executable_parallel="t.py",
        properties=SCHEMA["properties"], defs=SCHEMA["$defs"],
        required=SCHEMA["required"], type="object", title="SD")
    w.task_manager.add_task(
        name="Reg [p==1.0]", base_name="Reg", package_name="p==1.0", version="1.0",
        parent_dir=str(tmp_path), executable_parallel="c.py",
        executable_non_parallel="i.py", kind="compound",
        schema_parallel=COMPOUND_P, schema_non_parallel=COMPOUND_NP,
        properties=COMPOUND_P["properties"], defs=None,
        required=COMPOUND_P["required"], type="object", title="Reg")
    for key in ("SD [p==1.0]", "Reg [p==1.0]"):
        w._add_task_tab(key)
    w.messages = messages
    return w


def _save_to(monkeypatch, path):
    monkeypatch.setattr(W.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))


def test_export_and_import_parallel(widget, tmp_path, monkeypatch):
    form = widget._task_ui["SD [p==1.0]"]["form"]
    form.set_values({"channel": {"identifier": "nanog"}, "label_name": "nuc"})
    path = tmp_path / "args-sd.json"
    _save_to(monkeypatch, path)
    widget._export_args("SD [p==1.0]")
    data = json.loads(path.read_text())
    assert data == {"args_parallel": {"channel": {"identifier": "nanog"},
                                      "label_name": "nuc"},
                    "args_non_parallel": None}

    form.reset()
    widget._import_args("SD [p==1.0]", path=str(path))
    assert form.get_args() == {"channel": {"identifier": "nanog"}, "label_name": "nuc"}


def test_compound_workflow_export_and_import(widget, tmp_path, monkeypatch):
    ui = widget._task_ui["Reg [p==1.0]"]
    ui["form_np"].set_values({"reference_acquisition": 1})
    ui["form"].set_values({"wavelength_id": "A01_C01", "level": 0})
    path = tmp_path / "wf.json"
    _save_to(monkeypatch, path)
    widget._export_args("Reg [p==1.0]", workflow=True)
    entry = json.loads(path.read_text())["task_list"][0]
    assert entry["task"] == {"pkg_name": "p", "version": "1.0", "name": "Reg"}
    assert entry["args_non_parallel"] == {"reference_acquisition": 1}
    assert entry["args_parallel"] == {"wavelength_id": "A01_C01", "level": 0}

    ui["form"].reset(), ui["form_np"].reset()
    widget._import_args("Reg [p==1.0]", path=str(path))
    assert ui["form_np"].get_args() == {"reference_acquisition": 1}
    assert ui["form"].get_args() == {"wavelength_id": "A01_C01", "level": 0}


def test_import_reports_problems(widget, tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"args_parallel": {
        "channel": {"identifier": "DAPI"}, "nope": 1, "zarr_url": "/x"}}))
    widget._import_args("SD [p==1.0]", path=str(path))
    status = widget._task_ui["SD [p==1.0]"]["status"].text()
    assert "unknown arguments ignored: nope" in status
    assert "zarr_url" in status

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "wf", "task_list": [
        {"task": {"name": "Other", "pkg_name": "p"}, "args_parallel": {}}]}))
    widget._import_args("SD [p==1.0]", path=str(bad))
    assert "no task 'SD'" in widget.messages[-1]


def test_export_buttons(widget, tmp_path, monkeypatch, qtbot):
    from qtpy.QtCore import Qt
    from qtpy.QtWidgets import QPushButton

    page = widget._task_ui["SD [p==1.0]"]["page"]
    buttons = {b.text(): b for b in page.findChildren(QPushButton)}
    assert "Export as workflow…" not in buttons
    path = tmp_path / "args.json"
    _save_to(monkeypatch, path)
    qtbot.mouseClick(buttons["Export"], Qt.LeftButton)
    assert "args_parallel" in json.loads(path.read_text())

    monkeypatch.setattr(W.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    qtbot.mouseClick(buttons["Import"], Qt.LeftButton)
    assert "Imported" in widget._task_ui["SD [p==1.0]"]["status"].text()
