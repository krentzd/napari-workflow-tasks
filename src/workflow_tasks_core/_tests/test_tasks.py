"""The task registry: run file paths, registering a manifest's tasks."""

import json

from workflow_tasks_core.tasks import (
    FractalTaskManager,
    docs_summary,
    load_manifest,
    register_manifest_tasks,
)

# --------------------------------------------------------------------------
# FractalTaskManager: where a task's run files go
# --------------------------------------------------------------------------


def test_task_manager_paths(tmp_path):
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


def test_write_args_replaces_old_output(tmp_path):
    tm = FractalTaskManager()
    tm.add_task(
        name="T [p==1]", base_name="T", version="1", package_name="p==1",
        parent_dir="/env/src", executable_parallel="t.py", properties={},
        defs={}, required=[], type="object", title="T",
    )
    tm.get_run_dir("T [p==1]").mkdir(parents=True)
    old_out = tm.get_out_json_path("T [p==1]")
    with open(old_out, "w") as f:
        f.write("{}")

    args_json, out_json = tm.write_args("T [p==1]", {"zarr_url": "/d/0"})
    assert args_json == tm.get_path_to_json("T [p==1]")
    assert out_json == old_out
    with open(args_json) as f:
        assert json.load(f) == {"zarr_url": "/d/0"}
    assert not (tmp_path / "tasks" / "p" / "1" / "T" / "out_file.json").exists()


# --------------------------------------------------------------------------
# register_manifest_tasks
# --------------------------------------------------------------------------


MANIFEST = {"task_list": [
    {"name": "Segment", "executable_parallel": "seg.py",
     "args_schema_parallel": {"title": "Seg", "type": "object",
                              "properties": {"a": {"type": "integer"}},
                              "required": ["a"]},
     "docs_info": "Segments."},
    {"name": "Measure", "executable_non_parallel": "measure.py",
     "args_schema_non_parallel": {"properties": {"zarr_urls": {},
                                                 "b": {"type": "string"}}}},
    {"name": "Convert", "executable_non_parallel": "init.py",
     "executable_parallel": "compute.py",
     "args_schema_non_parallel": {"properties": {"zarr_dir": {}}},
     "args_schema_parallel": {"properties": {}}},
]}


def test_register_manifest_tasks():
    tm = FractalTaskManager()
    registered = register_manifest_tasks(tm, MANIFEST, "p==1.0", "1.0", "/src")

    assert registered.keys == {"Segment": "Segment [p==1.0]",
                               "Measure": "Measure [p==1.0]"}
    assert registered.added == ["Segment [p==1.0]", "Measure [p==1.0]"]
    assert registered.skipped == ["Convert"]  # a converter

    seg = tm.get_task("Segment [p==1.0]")
    assert seg["kind"] == "parallel"
    assert seg["base_name"] == "Segment"
    assert seg["title"] == "Seg"
    assert seg["required"] == ["a"]
    assert seg["docs_info"] == "Segments."
    assert tm.get_executable_path("Segment [p==1.0]") == "/src/seg.py"

    measure = tm.get_task("Measure [p==1.0]")
    assert measure["kind"] == "non_parallel"
    # the non-parallel arguments are the ones shown
    assert measure["properties"] == {"zarr_urls": {}, "b": {"type": "string"}}
    assert measure["title"] == "Measure"


def test_register_manifest_tasks_twice_adds_nothing():
    tm = FractalTaskManager()
    register_manifest_tasks(tm, MANIFEST, "p==1.0", "1.0", "/src")
    again = register_manifest_tasks(tm, MANIFEST, "p==1.0", "1.0", "/src")
    assert again.added == []
    assert again.keys["Segment"] == "Segment [p==1.0]"


def test_load_manifest(tmp_path):
    path = tmp_path / "__FRACTAL_MANIFEST__.json"
    path.write_text(json.dumps(MANIFEST))
    assert load_manifest({"manifest_path": str(path)}, {}) == MANIFEST
    missing = {"manifest_path": str(tmp_path / "nope.json")}
    assert load_manifest(missing, {"task_list": []}) == {"task_list": []}


# --------------------------------------------------------------------------
# docs_summary
# --------------------------------------------------------------------------


def test_docs_summary():
    docs = "# Title\n\n- **Segments** nuclei\n\nwith `cellpose`.\n\nMore."
    assert docs_summary(docs) == "Segments nuclei with cellpose. More."
    assert docs_summary("x" * 400) == "x" * 300 + "…"
