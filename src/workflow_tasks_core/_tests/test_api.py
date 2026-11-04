"""The headless API: Catalog, Package and Task."""

import json
import stat
import sys

import numpy as np
import pytest

if sys.platform == "win32":
    pytest.skip("uses a POSIX shell script as fake pixi", allow_module_level=True)

from ngio import create_ome_zarr_from_array  # noqa: E402

from workflow_tasks_core import (  # noqa: E402
    Catalog,
    Package,
    SchemaError,
    Task,
    TaskFailed,
    api,
    install,
    paths,
    task_packages,
)
from workflow_tasks_core.manifests import VersionInfo  # noqa: E402

# --------------------------------------------------------------------------
# A fake installed package "fake-tasks" 1.0 with three tasks
# --------------------------------------------------------------------------

SEGMENT = r'''
import argparse, json, os, sys
import numpy as np
from ngio import open_ome_zarr_container
p = argparse.ArgumentParser(); p.add_argument("--args-json"); p.add_argument("--out-json")
a = p.parse_args(); args = json.load(open(a.args_json))
channel = args["channels"]["identifiers"][0]
print("segmenting", channel)
if channel == "FAIL":
    print("no such channel"); sys.exit(1)
name = args.get("label_name", "{channel_identifier}_segmented").format(
    channel_identifier=channel)
if not args.get("skip_label"):
    c = open_ome_zarr_container(args["zarr_url"])
    c.derive_label(name, overwrite=True).set_array(np.ones((16, 16), "uint32"))
masking = args.get("create_masking_roi_table", {})
if masking.get("mode") == "Create":
    table = masking.get("table_name", "{label_name}_masking_ROI_table").format(
        label_name=name)
    tables = os.path.join(args["zarr_url"], "tables")
    os.makedirs(os.path.join(tables, table), exist_ok=True)
    meta = os.path.join(tables, ".zattrs")
    names = json.load(open(meta))["tables"] if os.path.exists(meta) else []
    json.dump({"tables": names + [table]}, open(meta, "w"))
json.dump(None, open(a.out_json, "w"))
'''

PROJECT = r'''
import argparse, json, os, shutil
p = argparse.ArgumentParser(); p.add_argument("--args-json"); p.add_argument("--out-json")
a = p.parse_args(); args = json.load(open(a.args_json))
print("ARGS", json.dumps(args, sort_keys=True))
updates = []
for url in args["zarr_urls"]:
    new = url + "_" + args.get("suffix", "mip")
    shutil.copytree(url, new, dirs_exist_ok=True)
    updates.append({"zarr_url": new, "origin": url})
json.dump({"image_list_updates": updates}, open(a.out_json, "w"))
'''

DEFS = {
    "Channels": {
        "type": "object", "title": "Channels",
        "properties": {
            "identifiers": {"type": "array", "items": {"type": "string"},
                            "minItems": 1, "title": "Identifiers",
                            "description": "Channel labels.\nMore text."},
        },
        "required": ["identifiers"],
    },
    "Create": {"type": "object", "title": "Create", "properties": {
        "mode": {"const": "Create", "type": "string", "default": "Create"},
        "table_name": {"type": "string",
                       "default": "{label_name}_masking_ROI_table"}}},
    "Skip": {"type": "object", "title": "Skip", "properties": {
        "mode": {"const": "Skip", "type": "string", "default": "Skip"}}},
}

MANIFEST = {
    "manifest_version": "2",
    "task_list": [
        {
            "name": "Segment",
            "executable_parallel": "segment.py",
            "docs_info": "## Segment\nSegments nuclei.",
            "args_schema_parallel": {
                "type": "object", "title": "Segment",
                "properties": {
                    "zarr_url": {"type": "string"},
                    "channels": {"$ref": "#/$defs/Channels",
                                 "description": "Channels to segment."},
                    "label_name": {"type": "string",
                                   "default": "{channel_identifier}_segmented",
                                   "description": "Name of the label."},
                    "skip_label": {"type": "boolean", "default": False},
                    "create_masking_roi_table": {
                        "oneOf": [{"$ref": "#/$defs/Create"},
                                  {"$ref": "#/$defs/Skip"}],
                        "discriminator": {"propertyName": "mode", "mapping": {
                            "Create": "#/$defs/Create", "Skip": "#/$defs/Skip"}},
                        "default": {"mode": "Skip"},
                    },
                },
                "required": ["zarr_url", "channels"],
                "$defs": DEFS,
            },
        },
        {
            "name": "Project",
            "executable_non_parallel": "project.py",
            "args_schema_non_parallel": {
                "type": "object",
                "properties": {
                    "zarr_urls": {"type": "array", "items": {"type": "string"}},
                    "zarr_dir": {"type": "string"},
                    "suffix": {"type": "string", "default": "mip"},
                },
                "required": ["zarr_urls", "zarr_dir"],
            },
        },
        {
            "name": "Convert",
            "executable_non_parallel": "convert.py",
            "args_schema_non_parallel": {
                "type": "object",
                "properties": {"zarr_dir": {"type": "string"}},
                "required": ["zarr_dir"],
            },
        },
    ],
}


def _make_image(url, channels=("DAPI", "nanog")):
    create_ome_zarr_from_array(
        str(url), np.zeros((len(channels), 16, 16), "uint16"), xy_pixelsize=1.0,
        axes_names="cyx", channel_labels=list(channels), levels=1,
    )
    return str(url)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """ENVS_DIR with fake-tasks 1.0 installed; a fake pixi; an image."""
    monkeypatch.setattr(paths, "ENVS_DIR", tmp_path / "envs")
    monkeypatch.setattr(paths, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    src = tmp_path / "src"
    src.mkdir()
    (src / "segment.py").write_text(SEGMENT)
    (src / "project.py").write_text(PROJECT)
    (src / "__FRACTAL_MANIFEST__.json").write_text(json.dumps(MANIFEST))
    d = paths.env_dir("fake-tasks", "1.0")
    (d / ".pixi" / "envs").mkdir(parents=True)
    (d / "pixi.toml").write_text("")
    (d / install.ENTRY_FILE).write_text(json.dumps({
        "src_path": str(src),
        "manifest_path": str(src / "__FRACTAL_MANIFEST__.json"),
        "pixi_manifest_path": str(d / "pixi.toml"),
        "pixi_frozen": True, "version": "1.0",
    }))
    pixi = tmp_path / "pixi"
    pixi.write_text('#!/bin/sh\nwhile [ "$1" != python ]; do shift; done\nshift\n'
                    f'exec {sys.executable} "$@"\n')
    pixi.chmod(pixi.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(api, "find_pixi", lambda: str(pixi))
    plate = tmp_path / "data" / "plate.zarr"
    return {"url": _make_image(plate / "B" / "03" / "0"), "plate": plate,
            "tmp": tmp_path}


@pytest.fixture
def segment(env):
    return Package.installed("fake-tasks").task("Segment")


# --------------------------------------------------------------------------
# Package
# --------------------------------------------------------------------------


def test_installed_package(env):
    pkg = Package.installed("fake_tasks")  # name is normalized
    assert (pkg.name, pkg.version, pkg.is_installed) == ("fake-tasks", "1.0", True)
    assert pkg.tasks == ["Segment", "Project", "Convert"]
    assert repr(pkg) == "<Package fake-tasks 1.0 (installed)>"


def test_installed_package_picks_newest(env):
    entry = json.loads((paths.env_dir("fake-tasks", "1.0") / install.ENTRY_FILE).read_text())
    for version in ("0.9", "1.10"):
        d = paths.env_dir("fake-tasks", version)
        (d / ".pixi" / "envs").mkdir(parents=True)
        (d / "pixi.toml").write_text("")
        (d / install.ENTRY_FILE).write_text(json.dumps({**entry, "version": version}))
    assert Package.installed("fake-tasks").version == "1.10"
    assert Package.installed("fake-tasks", "0.9").version == "0.9"
    assert [str(p) for p in Catalog.installed()] == [
        "fake-tasks 0.9", "fake-tasks 1.0", "fake-tasks 1.10"]


def test_installed_package_missing(env):
    with pytest.raises(FileNotFoundError, match="other-tasks is not installed"):
        Package.installed("other-tasks")


def test_unknown_task_suggests_name(env):
    with pytest.raises(KeyError, match="did you mean 'Segment'"):
        Package.installed("fake-tasks").task("segment")


def _catalog_package(versions=("1.0", "0.9")):
    return task_packages.TaskPackage(
        "https://pypi.org/project/fake-tasks", "pypi", "fake-tasks",
        [task_packages.PackageVersion(v, f"https://x/fake_tasks-{v}-py3-none-any.whl",
                                      f"fake_tasks-{v}-py3-none-any.whl")
         for v in versions])


def _info():
    return VersionInfo(MANIFEST, "fake_tasks", "fake_tasks/__FRACTAL_MANIFEST__.json",
                       [], None)


def test_package_before_install(env, monkeypatch):
    monkeypatch.setattr(api.manifests, "get_version_info", lambda p, v: _info())
    pkg = Package(_catalog_package(), "0.9")
    assert (pkg.version, pkg.versions, pkg.is_installed) == ("0.9", ["1.0", "0.9"], False)
    assert pkg.tasks == ["Segment", "Project", "Convert"]
    task = pkg.task("Segment")
    task.validate({"channels": {"identifiers": ["DAPI"]}})  # works uninstalled
    with pytest.raises(RuntimeError, match=r"fake-tasks 0.9 is not installed: "
                                           r"call pkg.install\(\) first"):
        task.run(env["url"], channels={"identifiers": ["DAPI"]}, log=None)


def test_package_unknown_version():
    with pytest.raises(KeyError, match="has no version 3.0"):
        Package(_catalog_package(), "3.0")


def test_install_returns_the_package(env, monkeypatch):
    monkeypatch.setattr(api.manifests, "get_version_info", lambda p, v: _info())
    calls = []

    def fake_install(pkg, version, extras=None, log=print, pins=None, use_lock=True):
        calls.append((pkg.pkg_name, version, extras, pins, use_lock))
        log("installing")
        return install.installed_entry("fake-tasks", "1.0")

    monkeypatch.setattr(api.install, "install_version", fake_install)
    pkg = Package(_catalog_package())
    lines = []
    assert pkg.install(log=lines.append, pins=["numpy<2"]) is pkg
    assert calls == [("fake-tasks", "1.0", None, ["numpy<2"], True)]
    assert lines == ["installing"]
    assert pkg.task("Segment").run(env["url"], channels={"identifiers": ["DAPI"]},
                                   log=None).outputs("labels")


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_package_by_name(monkeypatch):
    sources = ["https://pypi.org/project/fractal-tasks-core",
               "https://github.com/org/Fake_Tasks"]
    monkeypatch.setattr(api.task_packages, "fetch_sources", lambda: sources)
    loaded = []
    monkeypatch.setattr(api.task_packages, "load_package",
                        lambda s: loaded.append(s) or _catalog_package())
    pkg = Catalog().package("fake-tasks")
    assert loaded == ["https://github.com/org/Fake_Tasks"]
    assert (pkg.name, pkg.version) == ("fake-tasks", "1.0")
    with pytest.raises(KeyError, match="did you mean 'fractal-tasks-core'"):
        Catalog().package("fractal-task-core")


def test_catalog_search(monkeypatch):
    def fake_iter(live=False):
        yield "snapshot", "2026-11-04"
        yield "package", (_catalog_package(), _info())
        yield "error", ("https://broken", "HTTPError")

    monkeypatch.setattr(api.snapshot, "iter_catalog", fake_iter)
    catalog = Catalog()
    assert catalog.search("segm") == [
        {"package": "fake-tasks", "version": "1.0", "task": "Segment",
         "kind": "parallel"}]
    # a package name match lists all its tasks
    assert [r["task"] for r in catalog.search("FAKE")] == [
        "Segment", "Project", "Convert"]
    assert catalog.errors == [("https://broken", "HTTPError")]
    # the loaded catalog is reused
    assert catalog.package("fake-tasks").version == "1.0"


# --------------------------------------------------------------------------
# Task: arguments
# --------------------------------------------------------------------------


def test_task_basics(segment):
    assert isinstance(segment, Task)
    assert (segment.name, segment.kind) == ("Segment", "parallel")
    assert segment.key == "Segment [fake-tasks==1.0]"
    assert repr(segment) == "<Task 'Segment' (fake-tasks 1.0, parallel)>"


def test_arguments(segment):
    args = segment.arguments
    assert list(args) == ["channels", "label_name", "skip_label",
                          "create_masking_roi_table"]  # zarr_url is reserved
    assert args["channels"] == {"type": "Channels", "default": None,
                                "required": True,
                                "description": "Channels to segment."}
    assert args["label_name"]["default"] == "{channel_identifier}_segmented"
    assert args["create_masking_roi_table"]["type"] == "Create | Skip"


def test_help(segment, capsys):
    segment.help()
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Segment (fake-tasks 1.0, parallel)"
    assert "Segments nuclei." in out
    lines = [ln.split() for ln in out.splitlines()]
    assert ["channels", "Channels", "Channels", "to", "segment."] in lines
    # fields of a model argument, indented, first description line only
    assert ["identifiers", "list[str],", "required", "Channel", "labels."] in lines
    assert ["label_name", "str", "=", "'{channel_identifier}_segmented'",
            "Name", "of", "the", "label."] in lines
    assert out.index("required") < out.index("channels") < out.index("optional")
    # long defaults are shortened
    segment._task["args_schema_parallel"]["properties"]["label_name"]["default"] = "x" * 50
    segment.help()
    assert "label_name                   str = '…'" in capsys.readouterr().out


def test_validate(segment):
    segment.validate({"channels": {"identifiers": ["DAPI"]}})
    with pytest.raises(SchemaError) as info:
        segment.validate({"channel": {"identifiers": ["DAPI"]}, "skip_label": 1})
    assert [str(e) for e in info.value.errors] == [
        "channel: unknown argument (did you mean 'channels'?)",
        "skip_label: expected a boolean, got int",
        "channels: required",
    ]
    assert "channel: unknown argument" in str(info.value)


# --------------------------------------------------------------------------
# Task: running
# --------------------------------------------------------------------------


def test_run_parallel(env, segment):
    lines = []
    result = segment.run(env["url"], channels={"identifiers": ["DAPI"]},
                         create_masking_roi_table={"mode": "Create"},
                         log=lines.append)
    assert result is segment
    assert "segmenting DAPI" in lines
    assert segment.log == lines
    label = f"{env['url']}/labels/DAPI_segmented"
    table = f"{env['url']}/tables/DAPI_segmented_masking_ROI_table"
    assert segment.outputs("labels") == [label]
    assert segment.outputs("tables") == [table]
    assert segment.outputs("images") == []
    assert segment.outputs() == {"labels": [label], "tables": [table], "images": []}
    assert segment.zarr_urls == [env["url"]]
    assert (segment.missing, segment.failed) == ([], [])
    # run files go to the task's folder
    run_dir = paths.task_run_dir("fake-tasks", "1.0", "Segment")
    args = json.loads((run_dir / "image_1.args.json").read_text())
    assert args == {"zarr_url": env["url"], "channels": {"identifiers": ["DAPI"]},
                    "create_masking_roi_table": {"mode": "Create"}}


def test_run_args_dict_and_kwargs(env, segment):
    segment.run(env["url"], {"channels": {"identifiers": ["DAPI"]},
                             "label_name": "a"}, label_name="b", log=None)
    assert segment.outputs("labels") == [f"{env['url']}/labels/b"]


def test_rerun_with_existing_label_is_listed(env, segment):
    segment.run(env["url"], channels={"identifiers": ["DAPI"]}, log=None)
    segment.run(env["url"], channels={"identifiers": ["DAPI"]}, log=None)
    assert segment.outputs("labels") == [f"{env['url']}/labels/DAPI_segmented"]


def test_run_reports_missing_output(env, segment):
    segment.run(env["url"], channels={"identifiers": ["DAPI"]}, skip_label=True,
                log=None)
    assert segment.outputs("labels") == []
    assert segment.missing == [f"label 'DAPI_segmented' in {env['url']}"]


def test_run_several_images(env, segment):
    second = _make_image(env["plate"] / "B" / "03" / "1")
    segment.run([env["url"], second], channels={"identifiers": ["DAPI"]}, log=None)
    assert segment.outputs("labels") == [
        f"{env['url']}/labels/DAPI_segmented", f"{second}/labels/DAPI_segmented"]
    assert segment.zarr_urls == [env["url"], second]


def test_run_validates_first(env, segment):
    with pytest.raises(SchemaError, match="channels: required"):
        segment.run(env["url"], log=None)
    assert not paths.task_run_dir("fake-tasks", "1.0", "Segment").exists()


def test_run_missing_image(env, segment):
    with pytest.raises(FileNotFoundError, match="no OME-Zarr image"):
        segment.run(str(env["tmp"] / "nope.zarr"), channels={"identifiers": ["a"]},
                    log=None)


def test_run_failure_keeps_log_and_failed(env, segment):
    with pytest.raises(TaskFailed, match="no such channel"):
        segment.run(env["url"], channels={"identifiers": ["FAIL"]}, log=None)
    assert "no such channel" in segment.log
    assert segment.failed == [env["url"]]
    assert segment.outputs("labels") == []


def test_outputs_before_run(segment):
    with pytest.raises(RuntimeError, match="run the task first"):
        segment.outputs()


def test_outputs_unknown_kind(env, segment):
    segment.run(env["url"], channels={"identifiers": ["DAPI"]}, log=None)
    with pytest.raises(ValueError, match="kind must be one of"):
        segment.outputs("label")


def test_one_run_at_a_time(env, segment):
    segment._lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            segment.run(env["url"], channels={"identifiers": ["DAPI"]}, log=None)
    finally:
        segment._lock.release()


def test_run_non_parallel_and_chain(env):
    pkg = Package.installed("fake-tasks")
    project = pkg.task("Project")
    second = _make_image(env["plate"] / "B" / "03" / "1")
    lines = []
    project.run([env["url"], second], suffix="max", log=lines.append)
    (args,) = [json.loads(x[5:]) for x in lines if x.startswith("ARGS ")]
    assert args == {"zarr_urls": [env["url"], second],
                    "zarr_dir": str(env["tmp"] / "data"), "suffix": "max"}
    new = [env["url"] + "_max", second + "_max"]
    assert project.outputs("images") == new
    assert project.zarr_urls == [env["url"], second] + new

    # chain: run the next task on the projected images
    segment = pkg.task("Segment")
    segment.run(project.outputs("images"), channels={"identifiers": ["DAPI"]},
                log=None)
    assert segment.outputs("labels") == [f"{u}/labels/DAPI_segmented" for u in new]


def test_run_zarr_dir(env):
    project = Package.installed("fake-tasks").task("Project")
    lines = []
    project.run(env["url"], zarr_dir="/somewhere", log=lines.append)
    assert '"zarr_dir": "/somewhere"' in next(x for x in lines if x.startswith("ARGS"))


def test_converter_cannot_run(env):
    convert = Package.installed("fake-tasks").task("Convert")
    with pytest.raises(ValueError, match="creates new OME-Zarrs"):
        convert.run(env["url"], log=None)
