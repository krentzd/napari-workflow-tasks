"""Importing / exporting task arguments in Fractal's formats."""
import json
from typing import Any, Optional

import pytest

from workflow_tasks_core.args_io import (
    args_filename,
    export_args,
    export_workflow,
    parse_args,
    read_args,
    write_json,
)

from .test_api import env, segment  # noqa: F401  (fixtures)
from workflow_tasks_core import Package


def test_export_args_fractal_web_format():
    data = export_args({"a": 1, "zarr_url": "/x", "unset": None}, None)
    assert data == {"args_parallel": {"a": 1}, "args_non_parallel": None}
    assert args_filename("Cellpose SAM Segmentation") == "args-cellpose-sam-segmentation.json"


def test_export_workflow_is_valid_for_fractal_server():
    data = export_workflow("Calc Reg", "fractal-tasks-core", "1.4.0",
                           {"level": 2}, {"reference_acquisition": 0})
    # Same models as fractal-server's WorkflowImport (schemas/v2), which
    # forbid unknown keys
    pydantic = pytest.importorskip("pydantic")

    class TaskImport(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")
        pkg_name: str
        version: Optional[str] = None
        name: str

    class WorkflowTaskImport(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")
        meta_non_parallel: Optional[dict[str, Any]] = None
        meta_parallel: Optional[dict[str, Any]] = None
        args_non_parallel: Optional[dict[str, Any]] = None
        args_parallel: Optional[dict[str, Any]] = None
        type_filters: Optional[dict[str, bool]] = None
        input_filters: Optional[dict[str, Any]] = None
        description: Optional[str] = None
        alias: Optional[str] = None
        task: TaskImport

    class WorkflowImport(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")
        name: str
        description: Optional[str] = None
        task_list: list[WorkflowTaskImport]

    wf = WorkflowImport(**json.loads(json.dumps(data)))
    assert wf.task_list[0].task.version == "1.4.0"
    assert wf.task_list[0].args_parallel == {"level": 2}


def test_parse_args_file():
    got = parse_args({"args_parallel": {"x": 1, "zarr_url": "/a"},
                      "args_non_parallel": None})
    assert got.args_parallel == {"x": 1}
    assert got.args_non_parallel is None
    assert any("zarr_url" in n for n in got.notes)


@pytest.mark.parametrize("bad, message", [
    ([1, 2], "JSON object"),
    ({"foo": 1}, "does not contain task arguments"),
    ({"args_parallel": [1]}, "must be a JSON object"),
    ({"name": "wf", "task_list": []}, "has no tasks"),
])
def test_parse_args_errors(bad, message):
    with pytest.raises(ValueError, match=message):
        parse_args(bad, task_name="T")


def _workflow(*tasks):
    return {"name": "wf", "task_list": [
        {"args_parallel": args, "args_non_parallel": None,
         "task": {"pkg_name": pkg, "version": ver, "name": name}}
        for name, pkg, ver, args in tasks]}


def test_parse_workflow_picks_this_task():
    wf = _workflow(("Other", "p", "1", {"o": 1}),
                   ("T", "my_tasks", "1.0", {"t": 1}),
                   ("T", "my-tasks", "1.0", {"t": 2}))
    got = parse_args(wf, task_name="T", pkg_name="my-tasks", version="1.0")
    assert got.args_parallel == {"t": 1}
    assert any("2 'T' tasks" in n for n in got.notes)

    got = parse_args(wf, task_name="T", pkg_name="my-tasks", version="2.0")
    assert any("version 1.0" in n for n in got.notes)

    with pytest.raises(ValueError, match="no task 'Missing'.*Other, T"):
        parse_args(wf, task_name="Missing")


def test_api_round_trip_parallel(env, segment, tmp_path):  # noqa: F811
    path = tmp_path / "args.json"
    data = segment.write_args(path, channels={"identifiers": ["DAPI"]},
                              label_name="nuc")
    assert data == {"args_parallel": {"channels": {"identifiers": ["DAPI"]},
                                      "label_name": "nuc"},
                    "args_non_parallel": None}
    imported = segment.read_args(path)
    segment.run(env["url"], **imported.run_kwargs(), log=None)
    assert segment.outputs()["labels"] == [env["url"] + "/labels/nuc"]


def test_api_round_trip_workflow_non_parallel(env, tmp_path):  # noqa: F811
    project = Package.installed("fake-tasks").task("Project")
    path = tmp_path / "wf.json"
    data = project.write_args(path, suffix="max", workflow=True)
    entry = data["task_list"][0]
    assert entry["task"] == {"pkg_name": "fake-tasks", "version": "1.0", "name": "Project"}
    assert entry["args_non_parallel"] == {"suffix": "max"} and entry["args_parallel"] is None
    project.run(env["url"], **project.read_args(path).run_kwargs(), log=None)
    assert project.zarr_urls == [env["url"], env["url"] + "_max"]


def test_read_args_invalid_json(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{nope")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_args(p)
    write_json(p, {"args_parallel": {}})
    assert read_args(p).args_parallel == {}
