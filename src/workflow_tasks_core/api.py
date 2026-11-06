"""
Headless API: find a Fractal task package, install it and run its tasks on
OME-Zarr images, from a notebook or a script.

    from workflow_tasks_core import Catalog

    pkg = Catalog().package("fractal-cellpose-sam-task").install()
    seg = pkg.task("Cellpose SAM Segmentation")
    seg.help()
    seg.run("/data/plate.zarr/B/03/0", channels={"identifiers": ["DAPI"]})
    seg.outputs("labels")   # ['/data/plate.zarr/B/03/0/labels/DAPI_segmented']

Three classes:

* :class:`Catalog`: the Fractal task-package catalog (search, get a package)
  and the packages installed locally.
* :class:`Package`: one version of a task package; installs into its own
  pixi environment (``~/.napari_fractal_tasks/envs``).
* :class:`Task`: one task of a package: its arguments (``help``,
  ``validate``), ``run`` and, after a run, where its outputs are.

No napari here; :meth:`Task.show` imports the napari plugin when called.
"""

from __future__ import annotations

import difflib
import json
import os
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Optional, Union

from . import install, manifests, paths, snapshot, task_packages
from .manifests import VersionInfo, docs_summary, task_kind
from .outputs import (
    expected_image_urls,
    expected_label_names,
    expected_table_names,
    rewritten_inputs,
)
from .args_io import ImportedArgs, export_args, export_workflow, read_args, write_json
from .pixi_config import find_pixi
from .runner import (
    RUNNABLE_KINDS,
    TaskFailed,
    TaskJob,
    TaskResult,
    default_zarr_dir,
    image_urls,
    pixi_run_prefix,
    run_task_job,
)
from .schema import (
    RESERVED_ARGS,
    SchemaError,
    resolve,
    type_name,
    validate,
    with_defs,
)
from .task_packages import TaskPackage, normalize_package_name
from .zarr_meta import list_labels, list_tables

__all__ = ["Catalog", "Package", "Task"]

Images = Union[str, os.PathLike, Iterable[Union[str, os.PathLike]]]
LogFn = Optional[Callable[[str], None]]

OUTPUT_KINDS = ("labels", "tables", "images")


def _close_match_hint(name: str, choices: Iterable[str]) -> str:
    choices = list(choices)
    lowered = {c.lower(): c for c in choices}
    close = difflib.get_close_matches(name.lower(), list(lowered), n=1)
    return f" (did you mean {lowered[close[0]]!r}?)" if close else ""


def _first_line(text: Optional[str]) -> str:
    return (text or "").strip().split("\n", 1)[0].strip()


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


class Catalog:
    """The Fractal task-package catalog.

    ``package(name)`` loads only that package; ``search`` loads the whole
    catalog once (the published snapshot, else every source live, which
    takes a while). ``live=True`` always queries PyPI/GitHub directly.
    """

    def __init__(self, live: bool = False):
        self.live = live
        # Sources that could not be read by search(): (source, message)
        self.errors: list[tuple[str, str]] = []
        self._sources: Optional[list[str]] = None
        self._loaded: Optional[dict[str, tuple[TaskPackage, Optional[VersionInfo]]]] = None

    def _load(self) -> dict:
        if self._loaded is None:
            loaded, errors = {}, []
            for kind, value in snapshot.iter_catalog(live=self.live):
                if kind == "package":
                    pkg, info = value
                    loaded[pkg.pkg_name] = (pkg, info)
                elif kind == "error":
                    errors.append(tuple(value))
            self._loaded, self.errors = loaded, errors
        return self._loaded

    def search(self, text: str = "") -> list[dict]:
        """Tasks whose name, or whose package's name, contains ``text``
        (case-insensitive; "" lists every task). One dict per task:
        ``package``, ``version`` (newest stable), ``task``, ``kind``."""
        text = text.lower()
        rows = []
        for name, (pkg, info) in sorted(self._load().items()):
            if info is None or pkg.default_version is None:
                continue
            whole_package = text in name.lower()
            for task in info.task_list:
                if whole_package or text in task["name"].lower():
                    rows.append({
                        "package": name,
                        "version": pkg.default_version.version,
                        "task": task["name"],
                        "kind": task_kind(task),
                    })
        return rows

    def _source(self, name: str) -> str:
        if self._sources is None:
            self._sources = task_packages.fetch_sources()
        wanted = normalize_package_name(name)
        by_name = {
            normalize_package_name(s.rstrip("/").rsplit("/", 1)[-1]): s
            for s in self._sources
        }
        if wanted in by_name:
            return by_name[wanted]
        raise KeyError(
            f"No task package {name!r} in the catalog"
            + _close_match_hint(wanted, by_name)
        )

    def package(self, name: str, version: Optional[str] = None) -> "Package":
        """A package by name (or source URL); ``version`` defaults to the
        newest stable release."""
        wanted = normalize_package_name(name)
        if self._loaded is not None and wanted in self._loaded:
            pkg, info = self._loaded[wanted]
        else:
            source = name if "://" in name else self._source(name)
            pkg, info = task_packages.load_package(source), None
        default = pkg.default_version.version if pkg.default_version else None
        return Package(pkg, version,
                       info=info if version in (None, default) else None)

    @staticmethod
    def installed() -> list["Package"]:
        """Every package version installed locally (no network)."""
        found = []
        root = Path(paths.ENVS_DIR)
        if not root.is_dir():
            return found
        for pkg_dir in sorted(root.iterdir()):
            for version in install.installed_versions(pkg_dir.name):
                try:
                    found.append(Package.installed(pkg_dir.name, version))
                except FileNotFoundError:
                    continue  # broken install
        return found


# --------------------------------------------------------------------------
# Package
# --------------------------------------------------------------------------


class Package:
    """One version of a Fractal task package.

    Get one from :meth:`Catalog.package`, or with :meth:`Package.installed`
    for a version already installed (works offline).
    """

    def __init__(self, package: TaskPackage, version: Optional[str] = None, *,
                 entry: Optional[dict] = None,
                 info: Optional[VersionInfo] = None):
        self._pkg = package
        if version is None:
            if package.default_version is None:
                raise ValueError(f"{package.pkg_name} has no released version")
            version = package.default_version.version
        elif package.versions:
            package.get_version(version)  # KeyError if unknown
        self.version = version
        self._entry = entry
        self._info = info
        self._manifest: Optional[dict] = None

    @classmethod
    def installed(cls, name: str, version: Optional[str] = None) -> "Package":
        """An installed package (newest installed version by default),
        read from ``~/.napari_fractal_tasks/envs`` without the catalog."""
        pkg_name = normalize_package_name(name)
        versions = install.installed_versions(pkg_name)
        if not versions or (version is not None and version not in versions):
            what = pkg_name if version is None else f"{pkg_name} {version}"
            have = f" (installed: {', '.join(versions)})" if versions else ""
            raise FileNotFoundError(f"{what} is not installed{have}")
        version = version or versions[-1]
        entry = install.installed_entry(pkg_name, version)
        package = TaskPackage(source="", kind="installed", pkg_name=pkg_name)
        return cls(package, version, entry=entry)

    @classmethod
    def from_file(cls, path) -> "Package":
        """A task package from a source archive (``.tar.gz``, e.g. made with
        ``python -m build --sdist``) on this computer; ``install()`` it like
        a package from the catalog."""
        return cls(task_packages.local_package(path))

    # -- what it is --------------------------------------------------------

    @property
    def name(self) -> str:
        return self._pkg.pkg_name

    @property
    def key(self) -> str:
        """``"<name>==<version>"``."""
        return f"{self.name}=={self.version}"

    @property
    def source(self) -> str:
        return self._pkg.source

    @property
    def versions(self) -> list[str]:
        """Released versions, newest first."""
        return [v.version for v in self._pkg.versions] or [self.version]

    @property
    def is_installed(self) -> bool:
        return install.is_installed(self.name, self.version)

    @property
    def entry(self) -> Optional[dict]:
        """The install entry (paths of the env and the package), None if
        not installed."""
        if self._entry is None and self.is_installed:
            try:
                self._entry = install.installed_entry(self.name, self.version)
            except FileNotFoundError:
                return None
        return self._entry

    @property
    def info(self) -> VersionInfo:
        """Manifest and metadata of this version (from the catalog)."""
        if self._info is None:
            self._info = manifests.get_version_info(self._pkg, self.version)
        return self._info

    @property
    def manifest(self) -> dict:
        """The Fractal manifest: the installed one, else the catalog's."""
        if self._manifest is None:
            entry = self.entry
            if entry is not None:
                try:
                    with open(entry["manifest_path"]) as f:
                        self._manifest = json.load(f)
                except (OSError, ValueError, KeyError):
                    pass
            if self._manifest is None:
                self._manifest = self.info.manifest
        return self._manifest

    @property
    def tasks(self) -> list[str]:
        """Names of the package's tasks."""
        return [t["name"] for t in self.manifest.get("task_list", [])]

    def __str__(self) -> str:
        return f"{self.name} {self.version}"

    def __repr__(self) -> str:
        state = "installed" if self.is_installed else "not installed"
        return f"<Package {self.name} {self.version} ({state})>"

    # -- doing things ------------------------------------------------------

    def install(self, log: LogFn = print, extras: Optional[list[str]] = None,
                pins: Optional[list[str]] = None,
                use_lock: bool = True,
                skip: Optional[list[str]] = None) -> "Package":
        """Install this version into its own pixi environment (quick when
        already installed). ``log`` gets each line of the install log
        (None: silent); ``pins``: e.g. ``["numpy<2"]``; ``skip``:
        dependencies to leave out, e.g. ``["cupy-cuda12x"]``. Returns the
        package."""
        if not self._pkg.versions:  # from Package.installed: nothing to fetch
            if self.entry is None:
                raise RuntimeError(f"{self}: get it from Catalog().package() "
                                   "to install it")
            return self
        self._entry = install.install_version(
            self._pkg, self.version, extras=extras, log=log or (lambda _: None),
            pins=pins, use_lock=use_lock, skip=skip,
        )
        self._manifest = None  # read the installed manifest from now on
        return self

    def remove(self) -> None:
        """Delete this version's environment. Its install recipe is kept,
        so installing it again is quicker."""
        install.delete_env(self.name, self.version)
        self._entry = None
        self._manifest = None

    def task(self, name: str) -> "Task":
        """One of the package's tasks, by name (see ``tasks``)."""
        for task in self.manifest.get("task_list", []):
            if task["name"] == name:
                return Task(self, task)
        raise KeyError(f"{self} has no task {name!r}"
                       + _close_match_hint(name, self.tasks))


# --------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------


def _combined(errors: list[SchemaError]) -> SchemaError:
    """One SchemaError for several; ``.errors`` lists them all."""
    if len(errors) == 1:
        error = errors[0]
    else:
        error = SchemaError(errors[0].path, "")
        error.args = ("\n".join(str(e) for e in errors),)
    error.errors = errors
    return error


class Task:
    """One task of a :class:`Package`.

    Before a run: ``arguments``, ``help()``, ``validate(args)``.
    ``run(images, **args)`` runs it (in the package's pixi environment) and
    returns the task. After a run: ``outputs()`` (paths of the labels,
    tables and images it wrote), ``zarr_urls`` (the image list for the next
    task), ``missing``, ``failed``, ``rewritten``, ``log`` and ``show()``.
    """

    def __init__(self, package: Package, manifest_task: dict):
        self.package = package
        self._task = manifest_task
        self.name: str = manifest_task["name"]
        self.kind: str = task_kind(manifest_task)
        self.docs_info: str = manifest_task.get("docs_info") or ""
        self.docs_link: Optional[str] = manifest_task.get("docs_link")
        self._lock = threading.Lock()
        # State of the latest run
        self.log: list[str] = []
        self.zarr_urls: list[str] = []
        self.missing: list[str] = []
        self.failed: list[str] = []
        # Inputs the task rewrote in place (e.g. Apply Registration to
        # Image with overwrite_input): data read before the run is stale
        self.rewritten: list[str] = []
        self.inputs: list[str] = []
        self._outputs: Optional[dict] = None

    @property
    def key(self) -> str:
        """``"<task name> [<package>==<version>]"`` (as in the plugin)."""
        return f"{self.name} [{self.package.key}]"

    @property
    def run_dir(self) -> Path:
        """Where the run files (arguments and output JSON) go."""
        return paths.task_run_dir(self.package.name, self.package.version,
                                  self.name)

    def __repr__(self) -> str:
        return f"<Task {self.name!r} ({self.package}, {self.kind})>"

    # -- arguments -----------------------------------------------------------

    def args_schema(self, step: str) -> Optional[dict]:
        """The JSON Schema of the arguments of one step, ``"parallel"`` or
        ``"non_parallel"`` (the setup step of a compound task), with its
        ``$defs``; None if the task has no such step."""
        if step not in ("parallel", "non_parallel"):
            raise ValueError('step must be "parallel" or "non_parallel"')
        schema = self._task.get(f"args_schema_{step}")
        return with_defs(schema) if schema else None

    def _schemas(self) -> list[tuple[str, dict]]:
        """(step, schema) per step with arguments: "non_parallel" (the setup
        step of a compound task) first, then "parallel"."""
        steps = []
        for step in ("non_parallel", "parallel"):
            schema = self.args_schema(step)
            if schema:
                steps.append((step, schema))
        return steps

    def _merged_schema(self) -> dict:
        props, defs, required = {}, {}, []
        for _, schema in self._schemas():
            props.update(schema["properties"])
            defs.update(schema.get("$defs") or {})
            required += schema.get("required") or []
        return {"type": "object", "properties": props, "required": required,
                "$defs": defs}

    @property
    def arguments(self) -> dict:
        """The task's arguments (without those ``run`` fills in):
        name -> ``type``, ``default``, ``required``, ``description``."""
        result = {}
        for _, schema in self._schemas():
            defs = schema.get("$defs") or {}
            required = set(schema.get("required") or [])
            for name, prop in schema["properties"].items():
                if name in RESERVED_ARGS or name in result:
                    continue
                resolved = resolve(prop, defs)
                result[name] = {
                    "type": type_name(prop, defs),
                    "default": resolved.get("default"),
                    "required": name in required,
                    "description": (resolved.get("description") or "").strip(),
                }
        return result

    def help(self) -> None:
        """Print the task's description and arguments."""
        lines = [f"{self.name} ({self.package}, {self.kind})"]
        summary = docs_summary(self.docs_info) if self.docs_info else ""
        if summary:
            lines.append(summary)
        if self.docs_link:
            lines.append(self.docs_link)
        steps = self._schemas()
        titles = {
            "compound": {"non_parallel": "Setup step (runs once on all images)",
                         "parallel": "Compute step (runs once per image)"},
        }.get(self.kind, {})
        for step, schema in steps:
            defs = schema.get("$defs") or {}
            props = {k: v for k, v in schema["properties"].items()
                     if k not in RESERVED_ARGS}
            required = set(schema.get("required") or [])
            if step in titles:
                lines += ["", titles[step]]
            for heading, names in (
                ("required", [k for k in props if k in required]),
                ("optional", [k for k in props if k not in required]),
            ):
                if not names:
                    continue
                lines += ["", heading]
                for name in names:
                    lines += self._help_lines(name, props[name], defs, "  ")
                    resolved = resolve(props[name], defs)
                    sub_required = set(resolved.get("required") or [])
                    for sub, sub_prop in (resolved.get("properties") or {}).items():
                        lines += self._help_lines(sub, sub_prop, defs, "    ",
                                                  required=sub in sub_required)
        if not any(
            k not in RESERVED_ARGS for _, s in steps for k in s["properties"]
        ):
            lines += ["", "(no arguments)"]
        print("\n".join(lines))

    @staticmethod
    def _help_lines(name: str, prop: dict, defs: dict, indent: str,
                    required: bool = False) -> list[str]:
        resolved = resolve(prop, defs)
        text = type_name(prop, defs)
        if required:
            text += ", required"
        elif "default" in resolved:
            default = repr(resolved["default"])
            if len(default) > 40:  # e.g. a whole model's defaults
                default = default[0] + "…" + default[-1]
            text += f" = {default}"
        description = _first_line(resolved.get("description"))
        line = f"{indent}{name:<{30 - len(indent)}} {text}"
        return [f"{line}  {description}" if description else line]

    def _split(self, args: dict, non_parallel_args: Optional[dict]):
        """Arguments of the parallel and the non-parallel step."""
        if self.kind == "parallel":
            return dict(args), {}
        if self.kind == "non_parallel":
            return {}, {**args, **(non_parallel_args or {})}
        if non_parallel_args is not None:
            return dict(args), dict(non_parallel_args)
        np_props = with_defs(self._task.get("args_schema_non_parallel"))["properties"]
        p_props = with_defs(self._task.get("args_schema_parallel"))["properties"]
        args_p = {k: v for k, v in args.items() if k in p_props or k not in np_props}
        args_np = {k: v for k, v in args.items() if k in np_props}
        return args_p, args_np

    def _errors(self, args_p: dict, args_np: dict) -> list[SchemaError]:
        errors = []
        for step, values in (("non_parallel", args_np), ("parallel", args_p)):
            schema = self._task.get(f"args_schema_{step}")
            if schema:
                errors += validate(values, schema)
        return errors

    def read_args(self, path) -> ImportedArgs:
        """Arguments from a Fractal arguments file (fractal-web's export,
        ``{"args_parallel": ..., "args_non_parallel": ...}``) or a
        fractal-server workflow export (this task's entry). Use them as
        ``task.run(images, **task.read_args(path).run_kwargs())``; check
        ``.notes`` (e.g. a version mismatch)."""
        return read_args(path, task_name=self.name,
                         pkg_name=self.package.name,
                         version=self.package.version)

    def write_args(self, path, args: Optional[dict] = None, *,
                   non_parallel_args: Optional[dict] = None,
                   workflow: bool = False, **kwargs) -> dict:
        """Write arguments in Fractal's format: an arguments file (as
        fractal-web exports) or, with ``workflow=True``, a one-task
        fractal-server workflow that "Import workflow" accepts. Arguments
        are split between the steps like in :meth:`run`. Returns the JSON."""
        args_p, args_np = self._split({**(args or {}), **kwargs},
                                      non_parallel_args)
        has_p = self.kind in ("parallel", "compound")
        has_np = self.kind in ("non_parallel", "compound")
        args_p, args_np = (args_p if has_p else None), (args_np if has_np else None)
        if workflow:
            data = export_workflow(self.name, self.package.name,
                                   self.package.version, args_p, args_np)
        else:
            data = export_args(args_p, args_np)
        write_json(path, data)
        return data

    def validate(self, args: Optional[dict] = None,
                 non_parallel_args: Optional[dict] = None) -> None:
        """Check arguments against the task's schema; raises
        :class:`SchemaError` listing every problem (``.errors``)."""
        errors = self._errors(*self._split(args or {}, non_parallel_args))
        if errors:
            raise _combined(errors)

    # -- running -------------------------------------------------------------

    def _executable(self, entry: dict, step: str) -> Optional[str]:
        exe = self._task.get(f"executable_{step}")
        return os.path.join(entry["src_path"], exe) if exe else None

    @staticmethod
    def _image_urls(images: Images) -> list[str]:
        if isinstance(images, (str, os.PathLike)):
            images = [images]
        urls: list[str] = []
        for image in images:
            found = image_urls(os.fspath(image))
            if not found:
                raise FileNotFoundError(f"no OME-Zarr image at {image}")
            urls += [u for u in found if u not in urls]
        return urls

    def run(self, images: Images, args: Optional[dict] = None, *,
            log: LogFn = print, zarr_dir: Optional[str] = None,
            stop_on_error: bool = True,
            non_parallel_args: Optional[dict] = None, **kwargs) -> "Task":
        """Run the task on OME-Zarr image(s) and wait until it is done.

        ``images``: an image path, a well/plate path (all its images) or a
        list of paths. Arguments come from ``args`` and keyword arguments
        (these win); left-out arguments use the task's defaults. Each log
        line goes to ``log`` (None: silent) and to ``self.log``.

        Non-parallel and compound tasks: ``zarr_dir`` (where new OME-Zarrs
        go) defaults to the folder holding the images; arguments are
        split between the setup and the per-image step by their schemas,
        or given separately with ``non_parallel_args``.

        Raises :class:`SchemaError` for invalid arguments (nothing is run)
        and :class:`TaskFailed` if the task fails (``log`` and ``failed``
        are kept). Returns the task: see ``outputs()``.
        """
        lines = self.run_lines(images, args, zarr_dir=zarr_dir,
                               stop_on_error=stop_on_error,
                               non_parallel_args=non_parallel_args, **kwargs)
        for line in lines:
            if log is not None:
                log(line)
        return self

    def run_lines(self, images: Images, args: Optional[dict] = None, *,
                  zarr_dir: Optional[str] = None, stop_on_error: bool = True,
                  non_parallel_args: Optional[dict] = None,
                  **kwargs) -> Iterator[str]:
        """Like :meth:`run`, but returns the log lines one by one: the task
        runs while you iterate over them (e.g. in a GUI's worker thread).

        The task, its arguments and the images are checked right away, so
        those errors are raised by this call; :class:`TaskFailed` is
        raised while iterating."""
        if self.kind not in RUNNABLE_KINDS:
            raise ValueError(
                f"{self.name} is a {self.kind} task: it creates new OME-Zarrs "
                "from raw data and does not run on existing images")
        entry = self.package.entry
        if entry is None:
            raise RuntimeError(
                f"{self.package} is not installed: call pkg.install() first")
        args_p, args_np = self._split({**(args or {}), **kwargs},
                                      non_parallel_args)
        errors = self._errors(args_p, args_np)
        if errors:
            raise _combined(errors)
        urls = self._image_urls(images)
        if self._lock.locked():
            raise RuntimeError(f"{self.name} is already running")
        job = TaskJob(
            kind=self.kind,
            run_prefix=pixi_run_prefix(find_pixi(), entry),
            executable_parallel=self._executable(entry, "parallel"),
            executable_non_parallel=self._executable(entry, "non_parallel"),
            zarr_urls=urls,
            zarr_dir=zarr_dir or default_zarr_dir(urls),
            args_parallel=args_p,
            args_non_parallel=args_np,
            workdir=self.run_dir,
            stop_on_error=stop_on_error,
        )
        return self._run_job(job, {**args_np, **args_p})

    def _run_job(self, job: TaskJob, args: dict) -> Iterator[str]:
        """Run ``job``, yielding its log lines, then find what it wrote."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError(f"{self.name} is already running")
        try:
            urls = job.zarr_urls
            self.log, self.failed, self.missing, self.rewritten = [], [], [], []
            self.zarr_urls, self.inputs, self._outputs = [], urls, None
            before = {u: (set(list_labels(u)), set(list_tables(u))) for u in urls}
            try:
                result = yield from self._keep_log(run_task_job(job))
            except TaskFailed as e:
                result = e.result or TaskResult(failed=list(urls))
                self._collect(result, urls, before, args)
                raise
            self._collect(result, urls, before, args)
        finally:
            self._lock.release()

    def _keep_log(self, lines: Iterator[str]):
        """Pass the lines of a run on, keeping them in ``self.log``;
        returns what the run returns (its :class:`TaskResult`)."""
        while True:
            try:
                line = next(lines)
            except StopIteration as done:
                return done.value
            self.log.append(line)
            yield line

    def _collect(self, result: TaskResult, urls: list[str], before: dict,
                 args: dict) -> None:
        """Find what the run wrote (from its arguments and the disk)."""
        schema = self._merged_schema()
        props = schema["properties"]
        labels, tables, images, missing = [], [], [], []
        for url in dict.fromkeys(result.processed_zarr_urls):
            url_args = {**args, "zarr_url": url}
            labels_before, tables_before = before.get(url, (set(), set()))
            for found, before_run, expected, kind, out in (
                (list_labels(url), labels_before,
                 expected_label_names(url_args, props), "label", labels),
                (list_tables(url), tables_before,
                 expected_table_names(url_args, schema), "table", tables),
            ):
                on_disk = set(found)
                names = [n for n in expected if n in on_disk]
                names += sorted(on_disk - before_run - set(names))
                out += [f"{url.rstrip('/')}/{kind}s/{n}" for n in names]
                missing += [f"{kind} '{n}' in {url}"
                            for n in expected if n not in on_disk]
            for new in expected_image_urls(args, props, url):
                if os.path.isdir(new):
                    images.append(new)
                else:
                    missing.append(f"image {new}")
        inputs = {os.path.normpath(u) for u in urls}
        for update in result.image_list_updates:
            url = update.get("zarr_url")
            if url and os.path.normpath(url) not in inputs:
                images.append(url)
        images = list(dict.fromkeys(images))
        removed = set(result.image_list_removals)
        self.zarr_urls = [u for u in dict.fromkeys(urls + images)
                          if u not in removed]
        self.missing = missing
        self.failed = list(result.failed)
        self.rewritten = rewritten_inputs(result.image_list_updates, urls)
        self._outputs = {"labels": labels, "tables": tables, "images": images}

    # -- after a run -----------------------------------------------------------

    def outputs(self, kind: Optional[str] = None):
        """Paths of what the latest run wrote: ``"labels"``
        (``<image>/labels/<name>``), ``"tables"`` (``<image>/tables/<name>``)
        or ``"images"`` (new OME-Zarr images); all three as a dict if
        ``kind`` is None."""
        if self._outputs is None:
            raise RuntimeError(f"{self.name}: run the task first")
        if kind is None:
            return {k: list(v) for k, v in self._outputs.items()}
        if kind not in OUTPUT_KINDS:
            raise ValueError(
                f"kind must be one of {', '.join(map(repr, OUTPUT_KINDS))}")
        return list(self._outputs[kind])

    def show(self, viewer=None):
        """Open the input images with the labels this run wrote, and its new
        images, in napari: in ``viewer`` if given, else in the current
        viewer, else in a new one. Returns the viewer. Needs
        the napari plugin (napari-fractal-tasks) and napari installed."""
        self.outputs()  # raises if the task has not run yet
        try:
            from napari_fractal_tasks._show import show_task
        except ImportError as e:
            raise RuntimeError(
                "Task.show() needs napari and the napari-fractal-tasks "
                f"plugin ({e})") from e
        return show_task(self, viewer)
