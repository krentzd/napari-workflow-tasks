"""Running parallel / non-parallel / compound tasks (fractal-server style)."""
import json
import sys
from pathlib import Path

import pytest

from workflow_tasks_core.manifests import task_kind
from workflow_tasks_core.runner import (
    TaskFailed,
    TaskJob,
    default_zarr_dir,
    image_urls,
    pixi_run_prefix,
    run_task_job,
    short_name,
    stream_process,
)

SCRIPT = r"""
import argparse, json, sys
p = argparse.ArgumentParser(); p.add_argument("--args-json"); p.add_argument("--out-json")
a = p.parse_args(); args = json.load(open(a.args_json))
print("ARGS", json.dumps(args, sort_keys=True))
mode = sys.argv[0].split("/")[-1]
if mode == "init.py":
    items = [{"zarr_url": u, "init_args": {"ref": args["zarr_urls"][0]}} for u in args["zarr_urls"][1:]]
    json.dump({"parallelization_list": items + items}, open(a.out_json, "w"))
elif mode == "task.py":
    json.dump({"image_list_updates": []}, open(a.out_json, "w"))
elif mode == "fail.py":
    print("something broke"); sys.exit(1)
else:
    json.dump({"image_list_updates": [{"zarr_url": args["zarr_url"] + "_new"}]}, open(a.out_json, "w"))
"""


def _run(job):
    gen = run_task_job(job)
    lines = []
    while True:
        try:
            lines.append(next(gen))
        except StopIteration as stop:
            return stop.value, lines


@pytest.fixture
def scripts(tmp_path):
    for name in ("init.py", "compute.py", "task.py", "fail.py"):
        (tmp_path / name).write_text(SCRIPT)
    return tmp_path


def _job(scripts, kind, **kw):
    return TaskJob(
        kind=kind, run_prefix=[sys.executable],
        executable_parallel=str(scripts / "compute.py"),
        executable_non_parallel=str(scripts / "init.py"),
        zarr_urls=["/d/p.zarr/B/03/0", "/d/p.zarr/B/03/1", "/d/p.zarr/B/03/2"],
        zarr_dir="/d", workdir=scripts / "run", **kw,
    )


def _args(lines):
    return [json.loads(x[5:]) for x in lines if x.startswith("ARGS ")]


def test_compound_runs_setup_once_then_each_item(scripts):
    result, lines = _run(_job(scripts, "compound", args_parallel={"level": 2},
                              args_non_parallel={"ref_acq": 0}))
    setup, *computes = _args(lines)
    assert setup == {"zarr_urls": ["/d/p.zarr/B/03/0", "/d/p.zarr/B/03/1", "/d/p.zarr/B/03/2"],
                     "zarr_dir": "/d", "ref_acq": 0}
    assert len(computes) == 2  # duplicates removed
    assert computes[0] == {"zarr_url": "/d/p.zarr/B/03/1", "level": 2,
                           "init_args": {"ref": "/d/p.zarr/B/03/0"}}
    assert result.processed_zarr_urls == ["/d/p.zarr/B/03/1", "/d/p.zarr/B/03/2"]
    assert [u["zarr_url"] for u in result.image_list_updates] == [
        "/d/p.zarr/B/03/1_new", "/d/p.zarr/B/03/2_new"]


def test_parallel_runs_per_image(scripts):
    result, lines = _run(_job(scripts, "parallel", args_parallel={"x": 1}))
    assert [a["zarr_url"] for a in _args(lines)] == [
        "/d/p.zarr/B/03/0", "/d/p.zarr/B/03/1", "/d/p.zarr/B/03/2"]
    assert all("init_args" not in a for a in _args(lines))
    assert len(result.processed_zarr_urls) == 3


def test_non_parallel_runs_once(scripts):
    job = _job(scripts, "non_parallel")
    job.executable_non_parallel = str(scripts / "task.py")
    result, lines = _run(job)
    assert len(_args(lines)) == 1 and "zarr_urls" in _args(lines)[0]


def test_failure_stops_or_continues(scripts):
    job = _job(scripts, "parallel")
    job.executable_parallel = str(scripts / "fail.py")
    with pytest.raises(TaskFailed, match="something broke"):
        _run(job)
    assert not issubclass(TaskFailed, RuntimeError)  # superqt would swallow it

    job.stop_on_error = False
    result, _ = _run(job)
    assert len(result.failed) == 3 and not result.processed_zarr_urls


def _write_attrs(path: Path, attrs: dict):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".zattrs").write_text(json.dumps(attrs))


def test_image_urls_from_plate_well_image_and_label(tmp_path):
    plate = tmp_path / "p.zarr"
    _write_attrs(plate, {"plate": {"wells": [{"path": "B/03"}]}})
    _write_attrs(plate / "B/03", {"well": {"images": [{"path": "0"}, {"path": "1"}]}})
    for i in (0, 1):
        _write_attrs(plate / f"B/03/{i}", {"multiscales": [{}]})
    imgs = [str(plate / "B/03/0"), str(plate / "B/03/1")]
    assert image_urls(str(plate)) == imgs
    assert image_urls(str(plate / "B/03")) == imgs
    assert image_urls(imgs[0]) == imgs[:1]
    assert image_urls(imgs[1] + "/labels/nuclei") == imgs[1:]
    assert short_name(imgs[1]) == "p.zarr/B/03/1"
    assert default_zarr_dir(imgs) == str(tmp_path)


def test_task_kind():
    assert task_kind({"executable_parallel": "a"}) == "parallel"
    compound = {"executable_parallel": "a", "executable_non_parallel": "b",
                "args_schema_non_parallel": {"properties": {"zarr_urls": {}, "zarr_dir": {}}}}
    assert task_kind(compound) == "compound"
    assert task_kind({**compound, "category": "Conversion"}) == "converter_compound"
    assert task_kind({"executable_non_parallel": "b",
                      "args_schema_non_parallel": {"properties": {"zarr_dir": {}}}}) == "converter_non_parallel"
    assert task_kind({**compound, "type": "converter_compound"}) == "converter_compound"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def test_pixi_run_prefix():
    entry = {"pixi_manifest_path": "/env/pixi.toml"}
    assert pixi_run_prefix("pixi", entry) == [
        "pixi", "run", "--manifest-path", "/env/pixi.toml", "python"]
    entry["pixi_frozen"] = True
    assert pixi_run_prefix("pixi", entry) == [
        "pixi", "run", "--frozen", "--manifest-path", "/env/pixi.toml",
        "python"]


def test_stream_process_returns_exit_code():
    gen = stream_process([sys.executable, "-c",
                          "print('a'); print('b'); raise SystemExit(3)"])
    lines = []
    while True:
        try:
            lines.append(next(gen))
        except StopIteration as stop:
            returncode = stop.value
            break
    assert lines == ["a", "b"]
    assert returncode == 3
