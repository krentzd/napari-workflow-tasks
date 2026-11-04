"""
Run Fractal tasks locally, the way fractal-server's runner does
(``fractal_server/runner/v2/runner_functions.py``), on existing OME-Zarr
images.

* parallel:      ``executable_parallel`` once per image,
                 ``{"zarr_url": ..., **args_parallel}``
* non_parallel:  ``executable_non_parallel`` once,
                 ``{"zarr_urls": [...], "zarr_dir": ..., **args_non_parallel}``
* compound:      the non-parallel *setup* step once (as above); it returns a
                 ``parallelization_list``; then ``executable_parallel`` once per
                 item, ``{"zarr_url": item.zarr_url, "init_args":
                 item.init_args, **args_parallel}``

Converter tasks (whose setup step takes no ``zarr_urls``) create new
OME-Zarrs from raw data and are not run on existing images.

No Qt here: the plugin runs :func:`run_task_job` in a napari worker thread and
shows the yielded lines in the task's log.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Task kinds
# ---------------------------------------------------------------------------


RUNNABLE_KINDS = ("parallel", "non_parallel", "compound")


# ---------------------------------------------------------------------------
# OME-Zarr images behind a path
# ---------------------------------------------------------------------------


def _attrs(path: Path) -> dict:
    """NGFF attributes of a zarr group (v0.4 .zattrs or v0.5 zarr.json)."""
    try:
        with open(path / ".zattrs") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    try:
        with open(path / "zarr.json") as f:
            attrs = json.load(f).get("attributes", {})
        return attrs.get("ome", attrs)
    except (OSError, ValueError):
        return {}


def image_urls(path: Optional[str]) -> list[str]:
    """
    OME-Zarr image(s) behind a path (e.g. one opened in napari): an
    image, a label inside an image (``.../0/labels/nuclei``), a well, or a
    whole plate.
    """
    if not path:
        return []
    path = str(path).rstrip("/")
    if "/labels/" in path:  # label image -> its parent image
        path = path.split("/labels/")[0]
    root = Path(path)
    attrs = _attrs(root)
    if "multiscales" in attrs:
        return [path]
    if "well" in attrs:
        return [
            str(root / img["path"])
            for img in attrs["well"].get("images", [])
            if (root / img["path"]).exists()
        ]
    if "plate" in attrs:
        urls = []
        for well in attrs["plate"].get("wells", []):
            urls += image_urls(str(root / well["path"]))
        return urls
    return [path] if root.exists() else []


def short_name(zarr_url: str) -> str:
    """``.../plate.zarr/B/03/0`` -> ``plate.zarr/B/03/0``."""
    parts = Path(zarr_url).parts
    for i, part in enumerate(parts):
        if part.endswith(".zarr"):
            return "/".join(parts[i:])
    return Path(zarr_url).name


def default_zarr_dir(zarr_urls: list[str]) -> str:
    """
    Fractal's ``zarr_dir``: the folder containing the plates/images (every
    zarr_url must be inside it). New OME-Zarrs are written there.
    """
    parents = []
    for url in zarr_urls:
        parts = Path(url).parts
        idx = next((i for i, p in enumerate(parts) if p.endswith(".zarr")), None)
        parents.append(str(Path(*parts[:idx])) if idx else str(Path(url).parent))
    if not parents:
        return ""
    return os.path.commonpath(parents)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


class TaskFailed(Exception):
    """A task step failed.

    Not a RuntimeError on purpose: superqt's generator workers treat a
    RuntimeError as "worker deleted" and swallow it (no errored/finished).
    """


@dataclass
class TaskJob:
    kind: str
    run_prefix: list[str]           # e.g. ["pixi", "run", "--manifest-path", m, "python"]
    executable_parallel: Optional[str]
    executable_non_parallel: Optional[str]
    zarr_urls: list[str]
    zarr_dir: str
    args_parallel: dict = field(default_factory=dict)
    args_non_parallel: dict = field(default_factory=dict)
    workdir: Path = Path(".")
    stop_on_error: bool = True


@dataclass
class TaskResult:
    image_list_updates: list[dict] = field(default_factory=list)
    image_list_removals: list[str] = field(default_factory=list)
    processed_zarr_urls: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def pixi_run_prefix(pixi: str, entry: dict) -> list[str]:
    """Command prefix that runs ``python`` in the pixi environment of an
    installed package (catalog install ``entry``)."""
    frozen = ['--frozen'] if entry.get('pixi_frozen') else []
    return [pixi, 'run', *frozen, '--manifest-path',
            entry['pixi_manifest_path'], 'python']


def task_command(
    run_prefix: list[str], executable: str, args_json, out_json
) -> list[str]:
    """Command that runs a task executable with its arguments JSON and
    output JSON (``--args-json``, ``--out-json``)."""
    return [*run_prefix, executable,
            "--args-json", str(args_json), "--out-json", str(out_json)]


def stream_process(cmd: list[str]) -> Iterator[str]:
    """Run a command, yielding its output lines; returns the exit code."""
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    for line in proc.stdout:
        yield line.rstrip('\n')
    return proc.wait()


def _stream(cmd: list[str]) -> Iterator[str]:
    """Like :func:`stream_process`, but raises :class:`TaskFailed` with the
    last non-empty output line if the command fails, instead of returning
    the exit code."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    tail = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        tail = (tail + [line])[-15:]
        yield line
    rc = proc.wait()
    if rc != 0:
        last = next((x for x in reversed(tail) if x.strip()), f"exit code {rc}")
        raise TaskFailed(last)


def _run_step(
    job: TaskJob, executable: str, args: dict, name: str
) -> Iterator[str]:
    """Run one executable; yields log lines; returns its output JSON."""
    args_file = job.workdir / f"{name}.args.json"
    out_file = job.workdir / f"{name}.out.json"
    args_file.write_text(json.dumps(args, indent=1))
    if out_file.exists():
        out_file.unlink()
    cmd = task_command(job.run_prefix, executable, args_file, out_file)
    yield f"$ {' '.join(cmd)}"
    yield from _stream(cmd)
    if out_file.exists():
        try:
            return json.loads(out_file.read_text() or "null") or {}
        except ValueError:
            return {}
    return {}


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _merge(result: TaskResult, output: dict) -> None:
    result.image_list_updates += output.get("image_list_updates", []) or []
    result.image_list_removals += output.get("image_list_removals", []) or []


def _run_per_image(
    job: TaskJob, items: list[dict], result: TaskResult, label: str
) -> Iterator[str]:
    """The parallel part: once per (zarr_url, init_args) item."""
    n = len(items)
    for i, item in enumerate(items, 1):
        url = item["zarr_url"]
        args = {"zarr_url": url, **job.args_parallel}
        if "init_args" in item:
            args = {"zarr_url": url, "init_args": item["init_args"], **job.args_parallel}
        yield f"\n=== {label} {i}/{n}: {short_name(url)}"
        t0 = time.monotonic()
        try:
            output = yield from _run_step(job, job.executable_parallel, args, f"{label}_{i}")
        except TaskFailed as e:
            result.failed.append(url)
            yield f"✗ {short_name(url)} failed: {e}"
            if job.stop_on_error:
                raise
            continue
        _merge(result, output)
        result.processed_zarr_urls.append(url)
        yield f"✓ {short_name(url)} ({time.monotonic() - t0:.0f} s)"


def run_task_job(job: TaskJob) -> Iterator[str]:
    """
    Generator: yields log lines, returns a :class:`TaskResult`.
    Raises :class:`TaskFailed` when a step fails.
    """
    try:
        return (yield from _run_task_job(job))
    except RuntimeError as e:  # see TaskFailed
        raise TaskFailed(str(e)) from e


def _run_task_job(job: TaskJob) -> Iterator[str]:
    job.workdir.mkdir(parents=True, exist_ok=True)
    result = TaskResult()

    if job.kind == "parallel":
        items = [{"zarr_url": u} for u in job.zarr_urls]
        yield from _run_per_image(job, items, result, "image")

    elif job.kind in ("non_parallel", "compound"):
        setup_args = {
            "zarr_urls": list(job.zarr_urls),
            "zarr_dir": job.zarr_dir,
            **job.args_non_parallel,
        }
        step = "setup" if job.kind == "compound" else "task"
        yield f"=== {step} on {len(job.zarr_urls)} image(s)"
        output = yield from _run_step(job, job.executable_non_parallel, setup_args, step)

        if job.kind == "non_parallel":
            _merge(result, output)
            result.processed_zarr_urls = list(job.zarr_urls)
        else:
            items = _dedupe(output.get("parallelization_list", []) or [])
            for item in items:
                item.setdefault("init_args", {})
            yield f"Setup returned {len(items)} job(s)"
            if not items:
                yield "Nothing to compute."
            yield from _run_per_image(job, items, result, "compute")
    else:
        raise ValueError(
            f"Cannot run {job.kind} tasks on existing OME-Zarr images"
        )

    return result
