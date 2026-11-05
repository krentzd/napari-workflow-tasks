"""
Task lists (Fractal manifests) of task package versions.

The task list of a version is read from the ``__FRACTAL_MANIFEST__.json``
inside its wheel (like fractal-server's collection step), downloading only
the bytes needed, and cached on disk.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from email.parser import Parser
from pathlib import Path
from typing import Optional

# CATALOG_CACHE_DIR and http_get are used through their module (tests
# replace them)
from . import paths, task_packages
from .task_packages import TIMEOUT, USER_AGENT, TaskPackage, http_get_json


# --------------------------------------------------------------------------
# Manifests (cached per package/version; releases are immutable)
# --------------------------------------------------------------------------


@dataclass
class VersionInfo:
    """What we learn from a wheel: its manifest and some metadata."""

    manifest: dict
    module_name: str  # importable package that holds the manifest
    manifest_relpath: str  # e.g. "fractal_tasks_core/__FRACTAL_MANIFEST__.json"
    extras: list[str]
    requires_python: Optional[str]

    @property
    def task_list(self) -> list[dict]:
        return self.manifest.get("task_list", [])


def task_type(task: dict) -> str:
    p = task.get("executable_parallel")
    np_ = task.get("executable_non_parallel")
    if p and np_:
        return "compound"
    if p:
        return "parallel"
    if np_:
        return "non_parallel"
    return "unknown"


def task_kind(task: dict) -> str:
    """parallel | non_parallel | compound | converter_non_parallel |
    converter_compound (as in fractal-server's TaskType)."""
    p = task.get("executable_parallel")
    np_ = task.get("executable_non_parallel")
    if not p and not np_:
        return "unknown"
    # Recent manifests (fractal-task-tools) state the type explicitly
    if task.get("type") in (
        "parallel", "non_parallel", "compound",
        "converter_non_parallel", "converter_compound",
    ):
        return task["type"]
    if p and not np_:
        return "parallel"
    # Older manifests: converters don't take input images, or are in the
    # "Conversion" category (fractal-tasks-core 1.x converters still had an
    # unused `zarr_urls` argument)
    schema = task.get("args_schema_non_parallel") or {}
    converter = (
        "zarr_urls" not in (schema.get("properties") or {})
        or task.get("category") == "Conversion"
    )
    if p and np_:
        return "converter_compound" if converter else "compound"
    return "converter_non_parallel" if converter else "non_parallel"


class _HttpRangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range requests (enough for zipfile)."""

    BLOCK = 256 * 1024

    def __init__(self, url: str):
        self.url = url
        self.pos = 0
        self._blocks: dict[int, bytes] = {}
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            self.url = res.geturl()  # follow redirects once
            self.size = int(res.headers["Content-Length"])
            if res.headers.get("Accept-Ranges", "").lower() != "bytes":
                raise OSError("Server does not support range requests")

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def _block(self, i: int) -> bytes:
        if i not in self._blocks:
            start = i * self.BLOCK
            end = min(start + self.BLOCK, self.size) - 1
            req = urllib.request.Request(
                self.url,
                headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                if res.status != 206:
                    raise OSError("Range request not honored")
                self._blocks[i] = res.read()
        return self._blocks[i]

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = bytearray()
        while n > 0:
            i, off = divmod(self.pos, self.BLOCK)
            chunk = self._block(i)[off:off + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def read_remote_wheel_info(url: str, filename: str = "") -> VersionInfo:
    """Read manifest + METADATA of a remote wheel, downloading as little as
    possible (falls back to a full download without range support)."""
    try:
        return read_wheel_info(_HttpRangeFile(url), name=filename or url)
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as e:
        if isinstance(e, ValueError) and "__FRACTAL_MANIFEST__" in str(e):
            raise
        data = task_packages.http_get(url)
        return read_wheel_info(io.BytesIO(data), name=filename or url)


def summarize_info(info: VersionInfo) -> dict:
    """Small per-version summary stored in the snapshot index."""
    tasks = []
    for task in info.task_list:
        summary = {
            k: task.get(k)
            for k in (
                "name", "category", "modality", "tags",
                "executable_parallel", "executable_non_parallel",
            )
            if task.get(k) not in (None, [], "")
        }
        docs = task.get("docs_info") or ""
        if docs:
            summary["docs_info"] = docs[:400]
        # parallel / non_parallel / compound / converter_* (needs the schema,
        # which the summary does not keep)
        summary["kind"] = task_kind(task)
        tasks.append(summary)
    return {
        "module_name": info.module_name,
        "manifest_relpath": info.manifest_relpath,
        "extras": info.extras,
        "requires_python": info.requires_python,
        "tasks": tasks,
    }


def _cache_path(pkg_name: str, version: str) -> Path:
    return paths.CATALOG_CACHE_DIR / pkg_name / version / "info.json"


def read_wheel_info(wheel_path, name: str = "") -> VersionInfo:
    """``wheel_path``: a path or a seekable binary file object."""
    name = name or os.path.basename(str(wheel_path))
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        manifest_relpath = next(
            (n for n in names if n.endswith("/__FRACTAL_MANIFEST__.json")),
            None,
        )
        if manifest_relpath is None:
            raise ValueError(f"{name} has no __FRACTAL_MANIFEST__.json")
        manifest = json.loads(wheel.read(manifest_relpath))

        extras: list[str] = []
        requires_python = None
        metadata_name = next(
            (n for n in names if n.endswith(".dist-info/METADATA")), None
        )
        if metadata_name:
            meta = Parser().parsestr(wheel.read(metadata_name).decode())
            extras = meta.get_all("Provides-Extra") or []
            requires_python = meta.get("Requires-Python")

    module_name = os.path.dirname(manifest_relpath).replace("/", ".")
    return VersionInfo(
        manifest=manifest,
        module_name=module_name,
        manifest_relpath=manifest_relpath,
        extras=extras,
        requires_python=requires_python,
    )


def read_sdist_info(path) -> VersionInfo:
    """Manifest and metadata of a source archive (``.tar.gz``)."""
    meta = task_packages.sdist_metadata(path)
    with tarfile.open(path, "r:gz") as tar:
        member = next((m for m in tar.getmembers()
                       if m.name.endswith("/__FRACTAL_MANIFEST__.json")), None)
        if member is None:
            raise ValueError(
                f"{os.path.basename(str(path))} has no __FRACTAL_MANIFEST__.json")
        manifest = json.loads(tar.extractfile(member).read())
    # Its path in the installed package: without the archive's top folder
    # and the src/ folder of src layouts
    manifest_relpath = member.name.split("/", 1)[1]
    if manifest_relpath.startswith("src/"):
        manifest_relpath = manifest_relpath[len("src/"):]
    return VersionInfo(
        manifest=manifest,
        module_name=os.path.dirname(manifest_relpath).replace("/", "."),
        manifest_relpath=manifest_relpath,
        extras=meta["extras"],
        requires_python=meta["requires_python"],
    )


def get_version_info(pkg: TaskPackage, version: str) -> VersionInfo:
    """
    Full manifest + metadata for one version. Order: local cache, snapshot
    manifest file, the wheel itself (only manifest/METADATA bytes).
    """
    if pkg.kind == task_packages.LOCAL:
        # Not cached: the archive may be rebuilt under the same name
        return read_sdist_info(pkg.source)
    cache = _cache_path(pkg.pkg_name, version)
    if cache.exists():
        return VersionInfo(**json.loads(cache.read_text()))

    info = None
    if pkg.manifest_base:
        try:
            data = http_get_json(
                f"{pkg.manifest_base}{pkg.pkg_name}/{version}.json"
            )
            info = VersionInfo(**data)
        except Exception:  # noqa: BLE001
            info = None
    if info is None:
        pv = pkg.get_version(version)
        info = read_remote_wheel_info(pv.wheel_url, pv.wheel_filename)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(asdict(info)))
    return info


def get_display_info(pkg: TaskPackage, version: str) -> VersionInfo:
    """Task list to *display* for a version: the snapshot summary if
    available (no network), else the full manifest."""
    summary = pkg.summaries.get(version)
    if summary is None:
        return get_version_info(pkg, version)
    return VersionInfo(
        manifest={"task_list": summary.get("tasks", [])},
        module_name=summary.get("module_name", ""),
        manifest_relpath=summary.get("manifest_relpath", ""),
        extras=summary.get("extras", []),
        requires_python=summary.get("requires_python"),
    )


def docs_summary(docs: str) -> str:
    """The first lines of a task's ``docs_info`` as one short plain-text
    paragraph (headings and Markdown emphasis removed)."""
    paragraphs = [p.strip() for p in docs.split('\n\n')]
    lines = [ln for ln in '\n'.join(paragraphs).splitlines()
             if ln.strip() and not ln.lstrip().startswith('#')]
    first = ' '.join(
        re.sub(r'^\s*[-*]\s+', '', ln) for ln in lines[:3]
    ).strip()
    first = re.sub(r'(\*\*|__|`)', '', first)
    return first[:300] + ('…' if len(first) > 300 else '')
