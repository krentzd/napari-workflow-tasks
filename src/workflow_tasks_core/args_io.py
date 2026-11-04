"""
Import and export task arguments in the formats Fractal uses.

* **Arguments file** (fractal-web's "Export"/"Import" on a workflow task's
  arguments, ``args-<task-name>.json``)::

      {"args_parallel": {...} | null, "args_non_parallel": {...} | null}

* **Workflow file** (fractal-server's ``WorkflowExport`` / ``WorkflowImport``,
  ``GET .../workflow/{id}/export/``)::

      {"name": "...", "description": null,
       "task_list": [{"args_parallel": ..., "args_non_parallel": ...,
                      "meta_parallel": null, "meta_non_parallel": null,
                      "type_filters": {},
                      "task": {"pkg_name": "...", "version": "...",
                               "name": "..."}}]}

Importing accepts both: from a workflow file, the entry of the task in
question is used (matched by task name and package). Arguments that the
runner fills in (``zarr_url``, ``zarr_urls``, ``zarr_dir``, ``init_args``)
are never exported and are dropped on import.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Union

from .schema import RESERVED_ARGS

PathLike = Union[str, os.PathLike]


def _normalize(name: str) -> str:
    """PEP 503 package name normalization (as fractal-server)."""
    return re.sub(r"[-_.]+", "-", name or "").lower()


def _clean(args: Optional[dict]) -> Optional[dict]:
    """Drop reserved arguments and unset (None) values; None stays None."""
    if args is None:
        return None
    return {k: v for k, v in args.items()
            if k not in RESERVED_ARGS and v is not None}


def args_filename(task_name: str) -> str:
    """fractal-web's file name: ``args-<task-name-slug>.json``."""
    slug = re.sub(r"\s+", "-", task_name.strip().lower())
    return f"args-{slug}.json"


def export_args(args_parallel: Optional[dict],
                args_non_parallel: Optional[dict]) -> dict:
    """Arguments file content (fractal-web format).

    Use ``None`` for the step a task does not have (e.g. the non-parallel
    step of a parallel task)."""
    return {
        "args_parallel": _clean(args_parallel),
        "args_non_parallel": _clean(args_non_parallel),
    }


def export_workflow(
    task_name: str,
    pkg_name: str,
    version: str,
    args_parallel: Optional[dict],
    args_non_parallel: Optional[dict],
    workflow_name: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """A one-task workflow in fractal-server's export format, ready for
    "Import workflow" in Fractal (the task package must be collected there)."""
    return {
        "name": workflow_name or task_name,
        "description": description,
        "task_list": [{
            "meta_non_parallel": None,
            "meta_parallel": None,
            "args_non_parallel": _clean(args_non_parallel),
            "args_parallel": _clean(args_parallel),
            "type_filters": {},
            "task": {
                "pkg_name": pkg_name,
                "version": version,
                "name": task_name,
            },
        }],
    }


@dataclass
class ImportedArgs:
    """Arguments read from a file. ``None`` = the file has none for that
    step. ``notes``: things the user should know (e.g. version mismatch)."""

    args_parallel: Optional[dict] = None
    args_non_parallel: Optional[dict] = None
    notes: list[str] = field(default_factory=list)

    def run_kwargs(self) -> dict:
        """Keyword arguments for :meth:`workflow_tasks_core.Task.run`."""
        kwargs = {"args": dict(self.args_parallel or {})}
        if self.args_non_parallel is not None:
            kwargs["non_parallel_args"] = dict(self.args_non_parallel)
        return kwargs


def _pick_workflow_task(data: dict, task_name: Optional[str],
                        pkg_name: Optional[str], version: Optional[str],
                        notes: list[str]) -> dict:
    entries = data.get("task_list")
    if not isinstance(entries, list) or not entries:
        raise ValueError("The workflow file has no tasks")
    names = [((e.get("task") or {}).get("name"), e) for e in entries
             if isinstance(e, dict)]
    if task_name is None:
        if len(names) != 1:
            raise ValueError(
                "The workflow file has several tasks; say which one to import")
        return names[0][1]

    matches = [e for name, e in names if name == task_name]
    if pkg_name and matches:
        same_pkg = [e for e in matches
                    if _normalize((e.get("task") or {}).get("pkg_name", ""))
                    == _normalize(pkg_name)]
        if same_pkg:
            matches = same_pkg
        else:
            other = (matches[0].get("task") or {}).get("pkg_name")
            notes.append(f"the workflow uses '{task_name}' from {other}, "
                         f"not {pkg_name}")
    if not matches:
        found = ", ".join(sorted({n for n, _ in names if n})) or "none"
        raise ValueError(
            f"The workflow file has no task '{task_name}' (tasks: {found})")
    if len(matches) > 1:
        notes.append(f"the workflow has {len(matches)} '{task_name}' tasks; "
                     "used the first")
    entry = matches[0]
    wf_version = (entry.get("task") or {}).get("version")
    if version and wf_version and wf_version != version:
        notes.append(f"arguments are from version {wf_version}, "
                     f"this task is {version}")
    return entry


def parse_args(
    data,
    task_name: Optional[str] = None,
    pkg_name: Optional[str] = None,
    version: Optional[str] = None,
) -> ImportedArgs:
    """Read arguments from an arguments file or a workflow file (already
    parsed JSON). Raises ValueError with a readable message otherwise."""
    if not isinstance(data, dict):
        raise ValueError("The file does not contain a JSON object")
    notes: list[str] = []
    if "task_list" in data:
        data = _pick_workflow_task(data, task_name, pkg_name, version, notes)
    elif "args_parallel" not in data and "args_non_parallel" not in data:
        raise ValueError(
            "The file does not contain task arguments (it must have a "
            "top-level args_parallel or args_non_parallel, or be a Fractal "
            "workflow export)")

    result = ImportedArgs(notes=notes)
    for step in ("parallel", "non_parallel"):
        args = data.get(f"args_{step}")
        if args is not None and not isinstance(args, dict):
            raise ValueError(f"args_{step} must be a JSON object or null")
        if args:
            dropped = sorted(k for k in args if k in RESERVED_ARGS)
            if dropped:
                notes.append("ignored arguments filled in at run time: "
                             + ", ".join(dropped))
        setattr(result, f"args_{step}", _clean(args))
    return result


def read_args(path: PathLike, **kwargs) -> ImportedArgs:
    """:func:`parse_args` on a JSON file."""
    try:
        with open(path) as f:
            data = json.load(f)
    except ValueError as e:
        raise ValueError(f"{os.path.basename(os.fspath(path))} is not valid "
                         f"JSON ({e})") from e
    return parse_args(data, **kwargs)


def write_json(path: PathLike, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
