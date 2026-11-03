"""
Catalog of Fractal task packages and their available versions.

Mirrors how the Fractal ecosystem discovers tasks:

* The list of known task packages is the same ``sources.txt`` used to build
  the public Fractal task list
  (fractal-analytics-platform.github.io/tasks_data_retrieval/sources.txt).
  Entries are either PyPI projects or GitHub repositories.
* Versions come from the PyPI "simple" JSON API (like fractal-server's
  ``get_package_version_from_pypi``) or from GitHub releases with wheel
  assets. They are sorted newest-first with ``packaging.version.Version``
  (like fractal-server's ``_version_sort_key``).
* The task list of a version is read from the ``__FRACTAL_MANIFEST__.json``
  inside its wheel (like fractal-server's collection step), and cached on disk.
* Installation pins the exact version (``pkg==X`` / the wheel URL) inside a
  dedicated pixi environment, one per (package, version).

Like the Fractal task page, a pre-built snapshot of the catalog (every
version of every package, with task summaries) is published daily by a
GitHub Action (``scripts/build_catalog.py``). The plugin loads that snapshot
with a single request and only falls back to querying PyPI/GitHub live when
it is unavailable, so it does not spend the GitHub API rate limit.

This module has no Qt dependency so it can be used and tested headless.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from email.parser import Parser
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

SOURCES_URL = (
    "https://raw.githubusercontent.com/fractal-analytics-platform/"
    "fractal-analytics-platform.github.io/main/tasks_data_retrieval/sources.txt"
)

# Fallback used when SOURCES_URL cannot be reached (snapshot 2026-11-03)
DEFAULT_SOURCES = [
    "https://pypi.org/project/fractal-tasks-core",
    "https://pypi.org/project/fractal-faim-ipa",
    "https://pypi.org/project/fractal-lif-converters",
    "https://pypi.org/project/operetta-compose",
    "https://pypi.org/project/zmb-fractal-tasks",
    "https://pypi.org/project/apx-fractal-task-collection/",
    "https://pypi.org/project/fractal-uzh-converters/",
    "https://github.com/fractal-analytics-platform/fractal-helper-tasks",
    "https://github.com/fmi-basel/gliberal-scMultipleX",
    "https://github.com/m-albert/fractal-ome-zarr-hcs-stitching",
    "https://github.com/pelkmanslab/abbott",
    "https://github.com/pelkmanslab/abbott-features",
    "https://github.com/pelkmanslab/abbott-segmentation-tasks",
    "https://github.com/fractal-analytics-platform/fractal-cellpose-sam-task",
    "https://github.com/fractal-analytics-platform/fractal-ilastik-tasks",
    "https://github.com/fractal-analytics-platform/fractal-tasks-template",
    "https://github.com/fractal-analytics-platform/fractal-nd2-converters",
    "https://github.com/fractal-analytics-platform/fractal-czi-converters",
    "https://github.com/fractal-analytics-platform/fractal-microsam-segmentation-task",
    "https://github.com/fractal-analytics-platform/fractal-cellpose-2-segmentation-task",
    "https://github.com/fractal-analytics-platform/fractal-stardist-segmentation-task",
    "https://github.com/fractal-analytics-platform/fractal-napari-workflows",
    "https://github.com/ZMB-UZH/zmb-fractal-registration",
]

# Snapshot published by .github/workflows/build_catalog.yaml (branch `catalog`)
SNAPSHOT_URL = os.environ.get(
    "NAPARI_WORKFLOW_TASKS_CATALOG_URL",
    "https://raw.githubusercontent.com/rhornb/napari-workflow-tasks/catalog/",
)
SNAPSHOT_SCHEMA = 1

ROOT_DIR = Path.home() / ".napari_workflow_tasks"
CATALOG_CACHE_DIR = ROOT_DIR / "catalog"
ENVS_DIR = ROOT_DIR / "envs"

# Known-good dependency pins, pre-filled in the UI (like fractal-server's
# `pinned_package_versions`). The user can edit or clear them.
SUGGESTED_PINS = {
    # cellpose 4.2.0 removed `invert` from CellposeModel.eval(), which
    # fractal-cellpose-sam-task<=0.2.3 always passes.
    "fractal-cellpose-sam-task": ["cellpose<4.2"],
}

# Preferred Python versions for new environments, in order of preference
PREFERRED_PYTHONS = ["3.12", "3.11", "3.13", "3.10"]

TIMEOUT = 30
USER_AGENT = "napari-workflow-tasks"


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------


@dataclass
class PackageVersion:
    version: str
    wheel_url: str
    wheel_filename: str
    requires_python: Optional[str] = None
    yanked: bool = False

    @property
    def is_prerelease(self) -> bool:
        try:
            return Version(self.version).is_prerelease
        except InvalidVersion:
            return False


@dataclass
class TaskPackage:
    source: str
    kind: str  # "pypi" | "github"
    pkg_name: str  # normalized distribution name
    versions: list[PackageVersion] = field(default_factory=list)
    # From the snapshot: {version: summary dict} and where full manifests live
    summaries: dict = field(default_factory=dict)
    manifest_base: Optional[str] = None

    @property
    def default_version(self) -> Optional[PackageVersion]:
        """Newest non-yanked, non-prerelease version (else newest)."""
        for v in self.versions:
            if not v.yanked and not v.is_prerelease:
                return v
        return self.versions[0] if self.versions else None

    def get_version(self, version: str) -> PackageVersion:
        for v in self.versions:
            if v.version == version:
                return v
        raise KeyError(f"{self.pkg_name} has no version {version}")


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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def normalize_package_name(name: str) -> str:
    """PEP 503 normalization (same as fractal-server)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def version_sort_key(version: str):
    """Same ordering as fractal-server's ``_version_sort_key``."""
    try:
        return (2, Version(version))
    except InvalidVersion:
        return (1, version)


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


def _http_get(url: str, headers: Optional[dict] = None) -> bytes:
    req_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    req_headers.update(headers or {})
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        data = res.read()
        if res.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def _http_get_json(url: str, headers: Optional[dict] = None):
    return json.loads(_http_get(url, headers))


class GitHubRateLimitError(RuntimeError):
    """Raised when the GitHub API refuses requests because of rate limits."""


_GITHUB_TOKEN: Optional[str] = None
_GITHUB_TOKEN_LOOKED_UP = False


def _github_token() -> Optional[str]:
    """GITHUB_TOKEN / GH_TOKEN, else the token of a logged-in `gh` CLI."""
    global _GITHUB_TOKEN, _GITHUB_TOKEN_LOOKED_UP
    if not _GITHUB_TOKEN_LOOKED_UP:
        _GITHUB_TOKEN_LOOKED_UP = True
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            gh = shutil.which("gh") or next(
                (p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
                 if os.path.exists(p)),
                None,
            )
            if gh:
                try:
                    out = subprocess.run(
                        [gh, "auth", "token"],
                        capture_output=True, text=True, timeout=10,
                    )
                    token = out.stdout.strip() if out.returncode == 0 else None
                except (OSError, subprocess.SubprocessError):
                    token = None
        _GITHUB_TOKEN = token or None
    return _GITHUB_TOKEN


def _github_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Unauthenticated GitHub API calls are limited to 60/hour (5000 with a token)
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_get_json(url: str):
    """
    GET a GitHub API URL with an on-disk cache.

    Uses ETags: a "304 Not Modified" answer does not count against the rate
    limit. If GitHub refuses the request (rate limit) a cached answer is used
    when available.
    """
    import hashlib
    import time
    import urllib.error

    cache = CATALOG_CACHE_DIR / "_github" / (
        hashlib.sha1(url.encode()).hexdigest() + ".json"
    )
    cached = None
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
        except ValueError:
            cached = None

    headers = _github_headers()
    if cached and cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]

    req_headers = {"User-Agent": USER_AGENT, **headers}
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            data = json.loads(res.read())
            etag = res.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            return cached["data"]
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            if cached:
                return cached["data"]
            reset = e.headers.get("X-RateLimit-Reset")
            when = (
                time.strftime("%H:%M", time.localtime(int(reset)))
                if reset else "later"
            )
            raise GitHubRateLimitError(
                f"GitHub API rate limit reached (resets at {when}). "
                "Set GITHUB_TOKEN or log in with `gh auth login` "
                "to raise the limit."
            ) from e
        raise

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"etag": etag, "data": data}))
    return data


def _wheel_name_version(filename: str) -> tuple[str, str]:
    parts = filename[: -len(".whl")].split("-")
    return normalize_package_name(parts[0]), parts[1]


def _pick_wheel(filenames: list[str]) -> Optional[str]:
    wheels = [f for f in filenames if f.endswith(".whl")]
    if not wheels:
        return None
    # Prefer pure-python wheels
    pure = [w for w in wheels if w.endswith("-none-any.whl")]
    return (pure or wheels)[0]


# --------------------------------------------------------------------------
# Sources and versions
# --------------------------------------------------------------------------


def fetch_sources(url: str = SOURCES_URL) -> list[str]:
    """Read the official list of task-package sources (fallback: snapshot)."""
    try:
        text = _http_get(url).decode()
    except Exception:  # noqa: BLE001
        return list(DEFAULT_SOURCES)
    sources = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return sources or list(DEFAULT_SOURCES)


def _list_pypi_versions(project: str) -> list[PackageVersion]:
    data = _http_get_json(
        f"https://pypi.org/simple/{normalize_package_name(project)}/",
        headers={"Accept": "application/vnd.pypi.simple.v1+json"},
    )
    by_version: dict[str, list[dict]] = {}
    for f in data.get("files", []):
        if not f["filename"].endswith(".whl"):
            continue
        _, version = _wheel_name_version(f["filename"])
        by_version.setdefault(version, []).append(f)

    versions = []
    for version, files in by_version.items():
        chosen = _pick_wheel([f["filename"] for f in files])
        f = next(x for x in files if x["filename"] == chosen)
        versions.append(
            PackageVersion(
                version=version,
                wheel_url=f["url"],
                wheel_filename=f["filename"],
                requires_python=f.get("requires-python"),
                yanked=bool(f.get("yanked")),
            )
        )
    return versions


def _list_github_versions(owner: str, repo: str) -> list[PackageVersion]:
    releases = _github_get_json(
        f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100"
    )
    versions = []
    for release in releases:
        if release.get("draft"):
            continue
        assets = {a["name"]: a for a in release.get("assets", [])}
        chosen = _pick_wheel(list(assets))
        if chosen is None:
            continue
        _, version = _wheel_name_version(chosen)
        versions.append(
            PackageVersion(
                version=version,
                wheel_url=assets[chosen]["browser_download_url"],
                wheel_filename=chosen,
                yanked=False,
            )
        )
    return versions


def load_package(source: str) -> TaskPackage:
    """Get all versions (with a wheel) for one source, newest first."""
    parsed = urlparse(source)
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.hostname == "pypi.org" and len(parts) >= 2:
        kind = "pypi"
        pkg_name = normalize_package_name(parts[1])
        versions = _list_pypi_versions(parts[1])
    elif parsed.hostname == "github.com" and len(parts) >= 2:
        kind = "github"
        owner, repo = parts[0], parts[1]
        versions = _list_github_versions(owner, repo)
        if versions:
            pkg_name, _ = _wheel_name_version(versions[0].wheel_filename)
        else:
            pkg_name = normalize_package_name(repo)
    else:
        raise ValueError(f"Unsupported task source: {source}")

    # Several releases can ship the same wheel version: keep the first one
    unique: dict[str, PackageVersion] = {}
    for v in versions:
        unique.setdefault(v.version, v)
    versions = sorted(
        unique.values(),
        key=lambda v: version_sort_key(v.version),
        reverse=True,
    )
    return TaskPackage(
        source=source, kind=kind, pkg_name=pkg_name, versions=versions
    )


# --------------------------------------------------------------------------
# Manifests (cached per package/version; releases are immutable)
# --------------------------------------------------------------------------


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
        return read_wheel_info(io.BytesIO(_http_get(url)), name=filename or url)


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
        tasks.append(summary)
    return {
        "module_name": info.module_name,
        "manifest_relpath": info.manifest_relpath,
        "extras": info.extras,
        "requires_python": info.requires_python,
        "tasks": tasks,
    }


def _cache_path(pkg_name: str, version: str) -> Path:
    return CATALOG_CACHE_DIR / pkg_name / version / "info.json"


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


def get_version_info(pkg: TaskPackage, version: str) -> VersionInfo:
    """
    Full manifest + metadata for one version. Order: local cache, snapshot
    manifest file, the wheel itself (only manifest/METADATA bytes).
    """
    cache = _cache_path(pkg.pkg_name, version)
    if cache.exists():
        return VersionInfo(**json.loads(cache.read_text()))

    info = None
    if pkg.manifest_base:
        try:
            data = _http_get_json(
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


# --------------------------------------------------------------------------
# Snapshot (pre-built catalog, see scripts/build_catalog.py)
# --------------------------------------------------------------------------


def packages_from_index(index: dict, base_url: str = "") -> list[TaskPackage]:
    if index.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError(f"Unsupported catalog schema {index.get('schema')}")
    packages = []
    for p in index["packages"]:
        versions, summaries = [], {}
        for v in p["versions"]:
            summary = v.pop("summary", None)
            versions.append(PackageVersion(**v))
            if summary is not None:
                summaries[v["version"]] = summary
        packages.append(
            TaskPackage(
                source=p["source"],
                kind=p["kind"],
                pkg_name=p["pkg_name"],
                versions=versions,
                summaries=summaries,
                manifest_base=f"{base_url}manifests/" if base_url else None,
            )
        )
    return packages


def load_snapshot(base_url: str = SNAPSHOT_URL) -> tuple[str, list[TaskPackage]]:
    """
    Load the published catalog (one request). Falls back to the last
    downloaded copy when offline. Returns (generated_at, packages).
    """
    cache = CATALOG_CACHE_DIR / "snapshot_index.json"
    try:
        raw = _http_get(f"{base_url}index.json")
        index = json.loads(raw)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
    except Exception:  # noqa: BLE001
        if not cache.exists():
            raise
        index = json.loads(cache.read_text())
    return index.get("generated_at", "?"), packages_from_index(index, base_url)


# --------------------------------------------------------------------------
# Installation in a dedicated pixi environment
# --------------------------------------------------------------------------


def find_pixi() -> str:
    exe = shutil.which("pixi")
    if exe:
        return exe
    # napari launched from a GUI often doesn't inherit the shell PATH
    for candidate in (
        Path.home() / ".pixi" / "bin" / "pixi",
        Path.home() / ".pixi" / "bin" / "pixi.exe",
    ):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "Could not find the `pixi` executable. Install it from https://pixi.sh"
    )


def current_pixi_platform() -> str:
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return "osx-arm64" if arm else "osx-64"
    if sys.platform.startswith("win"):
        return "win-arm64" if arm else "win-64"
    return "linux-aarch64" if arm else "linux-64"


def choose_python(requires_python: Optional[str]) -> str:
    if not requires_python:
        return PREFERRED_PYTHONS[0]
    try:
        spec = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return PREFERRED_PYTHONS[0]
    for py in PREFERRED_PYTHONS:
        if Version(py + ".0") in spec or Version(py + ".99") in spec:
            return py
    return PREFERRED_PYTHONS[0]


def env_dir(pkg_name: str, version: str) -> Path:
    return ENVS_DIR / pkg_name / version


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(i) for i in items) + "]"


def parse_pins(pins: Optional[list[str]]) -> dict[str, str]:
    """["cellpose<4.2", "numpy==1.26.4"] -> {"cellpose": "<4.2", ...}"""
    parsed = {}
    for pin in pins or []:
        try:
            req = Requirement(pin)
        except InvalidRequirement as e:
            raise ValueError(f"Invalid pinned package {pin!r}: {e}") from e
        parsed[normalize_package_name(req.name)] = str(req.specifier) or "*"
    return parsed


def write_pixi_manifest(
    pkg: TaskPackage,
    version: str,
    extras: list[str],
    requires_python: Optional[str],
    pins: Optional[list[str]] = None,
) -> Path:
    """Write a pixi.toml that pins exactly this package version."""
    pv = pkg.get_version(version)
    py = choose_python(requires_python or pv.requires_python)

    dep_fields = []
    if pkg.kind == "pypi":
        dep_fields.append(f'version = "=={version}"')
    else:
        dep_fields.append(f"url = {json.dumps(pv.wheel_url)}")
    if extras:
        dep_fields.append(f"extras = {_toml_list(extras)}")

    content = (
        "# Generated by napari-workflow-tasks\n"
        "[workspace]\n"
        f'name = "{pkg.pkg_name}-{version}"\n'
        'channels = ["conda-forge"]\n'
        f'platforms = ["{current_pixi_platform()}"]\n'
        "\n[dependencies]\n"
        f'python = "{py}.*"\n'
        "\n[pypi-dependencies]\n"
        f'{pkg.pkg_name} = {{ {", ".join(dep_fields)} }}\n'
    )
    for name, spec in parse_pins(pins).items():
        if name != pkg.pkg_name:
            content += f"{name} = {json.dumps(spec)}\n"
    d = env_dir(pkg.pkg_name, version)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "pixi.toml"
    path.write_text(content)
    return path


def _run_streaming(cmd: list[str], log: Callable[[str], None]) -> str:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        lines.append(line)
        log(line)
    if proc.wait() != 0:
        tail = "\n".join(lines[-20:])
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{tail}")
    return "\n".join(lines)


def install_version(
    pkg: TaskPackage,
    version: str,
    extras: Optional[list[str]] = None,
    log: Callable[[str], None] = print,
    pins: Optional[list[str]] = None,
) -> dict:
    """
    Create/update the pixi env for (package, version) and locate the
    installed package. Returns a dict compatible with
    ``PackageImporter.loaded_packages`` entries.
    """
    info = get_version_info(pkg, version)
    extras = list(extras or [])
    pixi = find_pixi()
    manifest = write_pixi_manifest(
        pkg, version, extras, info.requires_python, pins=pins
    )

    log(f"Installing {pkg.pkg_name}=={version} into {manifest.parent}")
    _run_streaming([pixi, "install", "--manifest-path", str(manifest)], log)

    out = _run_streaming(
        [
            pixi,
            "run",
            "--manifest-path",
            str(manifest),
            "python",
            "-c",
            "import importlib.util, sys; "
            "s = importlib.util.find_spec(sys.argv[1]); "
            "print('PKGDIR=' + list(s.submodule_search_locations)[0])",
            info.module_name,
        ],
        log=lambda _line: None,
    )
    pkg_dir = next(
        line[len("PKGDIR="):] for line in out.splitlines()
        if line.startswith("PKGDIR=")
    )
    log(f"Installed {pkg.pkg_name}=={version} at {pkg_dir}")

    return {
        "src_path": pkg_dir,
        "manifest_path": os.path.join(pkg_dir, "__FRACTAL_MANIFEST__.json"),
        "pixi_manifest_path": str(manifest),
        "version": version,
    }


def is_installed(pkg_name: str, version: str) -> bool:
    d = env_dir(pkg_name, version)
    return (d / "pixi.toml").exists() and (d / ".pixi" / "envs").exists()


def iter_catalog(
    sources: Optional[list[str]] = None,
    max_workers: int = 8,
    live: bool = False,
    snapshot_url: Optional[str] = None,
) -> Iterator[tuple[str, object]]:
    """
    Yields ``("snapshot", generated_at)`` when the pre-built catalog was
    used, ``("package", (pkg, info))`` per package (``info`` = task list of
    the default version, or None) and ``("error", (source, message))``.

    With ``live=False`` (default) the published snapshot is used and no
    PyPI/GitHub API calls are made; if it is unreachable, falls back to live.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not live:
        try:
            generated_at, packages = load_snapshot(snapshot_url or SNAPSHOT_URL)
        except Exception as e:  # noqa: BLE001
            yield "error", ("catalog snapshot", f"unavailable ({e}); querying live")
        else:
            yield "snapshot", generated_at
            for pkg in packages:
                info = None
                if pkg.default_version is not None:
                    info = get_display_info(pkg, pkg.default_version.version)
                yield "package", (pkg, info)
            return

    sources = sources if sources is not None else fetch_sources()

    def _load(source):
        pkg = load_package(source)
        info = None
        if pkg.default_version is not None:
            info = get_version_info(pkg, pkg.default_version.version)
        return pkg, info

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_load, s): s for s in sources}
        for fut in as_completed(futures):
            source = futures[fut]
            try:
                yield "package", fut.result()
            except Exception as e:  # noqa: BLE001
                yield "error", (source, f"{type(e).__name__}: {e}")
