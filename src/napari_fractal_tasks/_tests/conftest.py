import json
import stat
import sys

import pytest

from workflow_tasks_core import Package, api, install, paths


@pytest.fixture(autouse=True)
def _own_root(tmp_path, monkeypatch):
    """Nothing is read from or written to the real ~/.napari_fractal_tasks:
    task run files, and the installed packages the widget lists."""
    monkeypatch.setattr(paths, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(paths, "ENVS_DIR", tmp_path / "envs")


@pytest.fixture
def fake_package(tmp_path, monkeypatch):
    """``fake_package(task_list, scripts)``: an installed package "p" 1.0
    with these manifest tasks and executables ({file name: source}); its
    tasks run with this Python through a fake pixi."""
    pixi = tmp_path / "pixi"
    pixi.write_text('#!/bin/sh\nwhile [ "$1" != python ]; do shift; done\nshift\n'
                    f'exec {sys.executable} "$@"\n')
    pixi.chmod(pixi.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(api, "find_pixi", lambda: str(pixi))

    def make(task_list, scripts=None) -> Package:
        src = tmp_path / "src"
        src.mkdir(exist_ok=True)
        for name, source in (scripts or {}).items():
            (src / name).write_text(source)
        manifest = src / "__FRACTAL_MANIFEST__.json"
        manifest.write_text(json.dumps({"manifest_version": "2",
                                        "task_list": task_list}))
        env = paths.env_dir("p", "1.0")
        (env / ".pixi" / "envs").mkdir(parents=True, exist_ok=True)
        (env / "pixi.toml").write_text("")
        (env / install.ENTRY_FILE).write_text(json.dumps({
            "src_path": str(src), "manifest_path": str(manifest),
            "pixi_manifest_path": str(env / "pixi.toml"),
            "pixi_frozen": True, "version": "1.0",
        }))
        return Package.installed("p")

    return make
