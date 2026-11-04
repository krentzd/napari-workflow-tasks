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
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from email.parser import Parser
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

from packaging import tags as packaging_tags
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

try:
    from . import _env_cache
except ImportError:  # loaded as a standalone file (scripts/build_catalog.py)
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "nwt_env_cache", Path(__file__).with_name("_env_cache.py")
    )
    _env_cache = _ilu.module_from_spec(_spec)
    sys.modules["nwt_env_cache"] = _env_cache
    _spec.loader.exec_module(_env_cache)


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

ROOT_DIR = Path(
    os.environ.get("NAPARI_WORKFLOW_TASKS_ROOT")
    or Path.home() / ".napari_workflow_tasks"
)
CATALOG_CACHE_DIR = ROOT_DIR / "catalog"
ENVS_DIR = ROOT_DIR / "envs"
# Install caches (see _env_cache): native-library store, install recipes,
# source archives. None = ENVS_DIR.parent / "cache" (see cache_dir())
CACHE_DIR: Optional[Path] = None

# Known-good dependency pins, pre-filled in the UI (like fractal-server's
# `pinned_package_versions`). The user can edit or clear them.
SUGGESTED_PINS = {
    # cellpose 4.2.0 removed `invert` from CellposeModel.eval(), which
    # fractal-cellpose-sam-task<=0.2.3 always passes.
    "fractal-cellpose-sam-task": ["cellpose<4.2"],
    # basicpy 2 (via scipy<1.13) forces numpy 1.26; pyarrow 26 needs numpy 2
    # at import time but does not declare it ("pyarrow requires NumPy 2.0").
    "zmb-fractal-tasks": ["pyarrow<26"],
    # zarr 2.18.6 does not cap numcodecs; numcodecs 0.16 removed
    # `blosc.cbuffer_sizes` that zarr 2 imports.
    "mantis-plex": ["numcodecs<0.16"],
    # tensorflow-metal 1.2.0 (pulled on macOS) fails to load with TF>=2.20
    # (dlopen _pywrap_tensorflow_internal.so); 2.19.1 verified.
    "operetta-compose": ["tensorflow<2.20"],
    # basicpy 2.0 removed FittingMode/ResizeMode imported by the tasks.
    "apx-fractal-task-collection": ["basicpy<2"],
    # polars 2 no longer broadcasts Series into an empty DataFrame
    # (ShapeError in abbott_features/features/_base.py).
    "abbott-features": ["polars<2"],
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
    # Source archive of the release (sdist / GitHub tag tarball); used to
    # install from the package's own pixi.lock, like fractal-server's pixi
    # task collection
    source_url: Optional[str] = None
    source_size: Optional[int] = None
    pixi_lock: Optional[bool] = None  # None = unknown until checked

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


def _wheel_tags(filename: str) -> frozenset:
    try:
        return parse_wheel_filename(filename)[3]
    except (InvalidWheelFilename, InvalidVersion):
        return frozenset()


def _supported_tags(python: str) -> set:
    """Wheel tags installable on this machine with CPython ``python`` ("3.11").

    Environments are always created for the host platform
    (see current_pixi_platform), so the host's platform tags are used.
    """
    pyver = tuple(int(x) for x in python.split(".")[:2])
    plats = list(packaging_tags.platform_tags())
    return set(packaging_tags.cpython_tags(pyver, platforms=plats)) | set(
        packaging_tags.compatible_tags(pyver, f"cp{pyver[0]}{pyver[1]}", plats)
    )


def wheel_is_compatible(filename: str, python: str) -> bool:
    """Whether the wheel can be installed on this machine with ``python``."""
    return bool(_wheel_tags(filename) & _supported_tags(python))


def _pick_wheel(
    filenames: list[str], python: Optional[str] = None
) -> Optional[str]:
    """
    Pick a wheel among ``filenames``.

    With ``python``: only a wheel installable on this machine with that
    Python (pure wheels first), or None. Without: the best wheel to read the
    manifest from (pure, else one compatible with this machine for a
    preferred Python, else any wheel -- the manifest is platform-neutral).
    """
    wheels = [f for f in filenames if f.endswith(".whl")]
    if not wheels:
        return None
    pure = [w for w in wheels if w.endswith("-none-any.whl")]
    if python is not None:
        return next(
            (w for w in pure + wheels if wheel_is_compatible(w, python)), None
        )
    if pure:
        return pure[0]
    for py in PREFERRED_PYTHONS:
        compatible = [w for w in wheels if wheel_is_compatible(w, py)]
        if compatible:
            return compatible[0]
    return wheels[0]


def _is_sdist(filename: str) -> bool:
    return filename.endswith((".tar.gz", ".zip"))


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
    sdists: dict[str, dict] = {}
    prefix = normalize_package_name(project).replace("-", "_")
    for f in data.get("files", []):
        name = f["filename"]
        if name.endswith(".tar.gz"):
            dist, _, ver = name[: -len(".tar.gz")].rpartition("-")
            if normalize_package_name(dist).replace("-", "_") == prefix:
                sdists[ver] = f
            continue
        if not name.endswith(".whl"):
            continue
        _, version = _wheel_name_version(name)
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
                source_url=sdists.get(version, {}).get("url"),
                source_size=sdists.get(version, {}).get("size"),
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
        name, version = _wheel_name_version(chosen)
        tag = release.get("tag_name")
        # Source: the release's sdist asset. GitHub's automatic tag archive
        # has no PKG-INFO, so packages versioned by hatch-vcs /
        # setuptools-scm can't be built from it; it is only the fallback.
        sdist = _release_sdist_asset(assets, name, version)
        if sdist is not None:
            source_url, source_size = sdist["browser_download_url"], sdist.get("size")
        elif tag:
            source_url = f"https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.tar.gz"
            source_size = None
        else:
            source_url = source_size = None
        versions.append(
            PackageVersion(
                version=version,
                wheel_url=assets[chosen]["browser_download_url"],
                wheel_filename=chosen,
                yanked=False,
                source_url=source_url,
                source_size=source_size,
            )
        )
    return versions


def _release_sdist_asset(assets: dict, name: str, version: str) -> Optional[dict]:
    """The ``<name>-<version>.tar.gz`` asset of a GitHub release (any
    spelling of the distribution name), or None."""
    for filename, asset in assets.items():
        if not filename.endswith(".tar.gz"):
            continue
        dist, _, ver = filename[: -len(".tar.gz")].rpartition("-")
        if normalize_package_name(dist) == name and ver == version:
            return asset
    return None


def _github_tag_from_source_url(url: Optional[str]):
    """(owner, repo, tag) of a GitHub tag archive or release-asset URL."""
    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/archive/refs/tags/(.+)\.tar\.gz$",
        url or "",
    ) or re.match(
        r"https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/[^/]+$",
        url or "",
    )
    return m.groups() if m else None


def has_pixi_lock(pv: PackageVersion) -> Optional[bool]:
    """
    Whether the release ships a pixi.lock. Cheap for GitHub releases (one
    request to raw.githubusercontent.com, not the API); None (unknown) for
    PyPI sdists, which have to be downloaded to tell.
    """
    if pv.pixi_lock is not None:
        return pv.pixi_lock
    gh = _github_tag_from_source_url(pv.source_url)
    if gh is None:
        return None
    owner, repo, tag = gh
    req = urllib.request.Request(
        f"https://raw.githubusercontent.com/{owner}/{repo}/{tag}/pixi.lock",
        method="HEAD",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            pv.pixi_lock = True
    except Exception:  # noqa: BLE001
        pv.pixi_lock = False
    return pv.pixi_lock


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
            known = PackageVersion.__dataclass_fields__
            versions.append(
                PackageVersion(**{k: x for k, x in v.items() if k in known})
            )
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


def candidate_pythons(requires_python: Optional[str]) -> list[str]:
    """PREFERRED_PYTHONS allowed by ``requires_python``, in preference order
    (all of them when it is missing/invalid or nothing matches)."""
    if not requires_python:
        return list(PREFERRED_PYTHONS)
    try:
        spec = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return list(PREFERRED_PYTHONS)
    allowed = [
        py for py in PREFERRED_PYTHONS
        if Version(py + ".0") in spec or Version(py + ".99") in spec
    ]
    return allowed or list(PREFERRED_PYTHONS)


def choose_python(requires_python: Optional[str]) -> str:
    return candidate_pythons(requires_python)[0]


# Virtual packages detected by pixi -> keys of a pixi platform entry
# (``platforms = [{ name = ..., platform = ..., macos = "26.7.1" }]``).
# pixi's detection honours CONDA_OVERRIDE_OSX / _GLIBC / _CUDA.
_VIRTUAL_TO_SYSREQ = {"__osx": "macos", "__glibc": "glibc", "__cuda": "cuda"}
# Operating systems (platform prefix) each requirement applies to
_SYSREQ_OS = {"macos": ("osx",), "osx": ("osx",), "glibc": ("linux",),
              "linux": ("linux",), "cuda": ("linux", "win"),
              "windows": ("win",)}

# System requirements on `platforms` entries ("rich platforms") replaced
# the `[system-requirements]` table in pixi 0.71.0, which deprecated the
# table (0.83 prints a warning for it on every install and run). Older
# pixi only knows the table: see use_rich_platforms().
RICH_PLATFORMS_MIN_PIXI = (0, 71, 0)
# Name of the platform entry carrying the host's requirements. pixi
# rejects an entry with virtual packages whose name is the bare platform
# ("give the entry a `name` distinct from its subdir"); the lock file is
# keyed by this name (see lock_platform_names).
HOST_PLATFORM_SUFFIX = "-host"


def host_system_requirements(
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    System requirements of this machine as detected by pixi
    (``pixi info --json`` -> ``virtual_packages``), as keys of a pixi
    platform entry, e.g. ``{"macos": "26.7.1"}`` or
    ``{"glibc": "2.28", "cuda": "12.4"}``.

    Without them pixi assumes conservative defaults (e.g. macOS 13.0 on
    osx-arm64) and rejects wheels built for newer systems (PyQt6 6.12 is
    macosx_14_0 only), then tries to build them from source.
    Returns {} if pixi cannot be queried.
    """
    try:
        out = subprocess.run(
            [find_pixi(), "info", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        virtual = json.loads(out.stdout).get("virtual_packages") or []
    except (OSError, subprocess.SubprocessError, ValueError,
            AttributeError) as e:
        if log:
            log(f"Warning: could not detect system requirements "
                f"(pixi info failed: {e}); using pixi defaults")
        return {}
    reqs: dict = {}
    for entry in virtual:
        name, _, rest = str(entry).partition("=")
        version = rest.split("=", 1)[0]
        key = _VIRTUAL_TO_SYSREQ.get(name)
        if not key or not version or version == "0":
            continue
        reqs[key] = version
    return reqs


def _parse_pixi_version(text: str) -> Optional[tuple]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def use_rich_platforms(pixi_version_text: str) -> bool:
    """Whether this pixi (``pixi --version`` output) takes system
    requirements on ``platforms`` entries. False for older or unknown
    versions: they get the ``[system-requirements]`` table, which every
    pixi understands (newer ones only warn)."""
    v = _parse_pixi_version(pixi_version_text)
    return v is not None and v >= RICH_PLATFORMS_MIN_PIXI


def sysreqs_for_platform(reqs: dict, platform_: str) -> dict:
    """The requirements of ``reqs`` that apply to ``platform_``."""
    os_ = platform_.split("-", 1)[0]
    return {k: v for k, v in reqs.items()
            if os_ in _SYSREQ_OS.get(k, (os_,))}


def sysreqs_from_table(table: dict) -> dict:
    """``[system-requirements]`` table -> platform-entry keys
    (``libc = { family = "glibc", version = "2.28" }`` -> ``glibc``)."""
    out = {}
    for k, v in (table or {}).items():
        if k == "libc":
            if isinstance(v, dict):
                if v.get("family", "glibc") != "glibc":
                    continue  # pixi platform entries know glibc only
                v = v.get("version")
            k = "glibc"
        if v is not None:
            out[k] = v
    return out


def sysreqs_to_table(reqs: dict) -> dict:
    """Platform-entry keys -> ``[system-requirements]`` table."""
    return {
        ("libc" if k == "glibc" else k):
        ({"family": "glibc", "version": str(v)} if k == "glibc" else v)
        for k, v in reqs.items()
    }


def host_platform_entry(platform_: str, reqs: dict, name: Optional[str] = None):
    """pixi ``platforms`` entry for ``platform_`` with ``reqs``: the bare
    platform when there are none, else a named table."""
    reqs = {k: str(v) if not isinstance(v, bool) else v
            for k, v in sysreqs_for_platform(reqs, platform_).items()}
    if not reqs:
        return platform_
    return {"name": name or platform_ + HOST_PLATFORM_SUFFIX,
            "platform": platform_, **reqs}


def _toml_value(value) -> str:
    """TOML literal for str/number/bool/list/dict values. Numbers become
    strings (pixi wants versions such as ``macos = "13.0"`` as strings)."""
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value))


def system_requirements_toml(reqs: dict) -> str:
    """`[system-requirements]` table for ``reqs`` ("" when empty); for pixi
    without rich platforms (see use_rich_platforms)."""
    if not reqs:
        return ""
    table = sysreqs_to_table(reqs)
    lines = "".join(f"{k} = {_toml_value(v)}\n" for k, v in table.items())
    return "\n[system-requirements]\n" + lines


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


# Declare the host's system requirements (host_system_requirements) in
# generated manifests. Set False to get pixi's defaults.
DECLARE_SYSTEM_REQUIREMENTS = True


@dataclass
class InstallSource:
    """What to put in pixi.toml for the task package itself."""

    dep_fields: list[str]
    pythons: list[str]  # candidate Pythons, in preference order
    description: str


def _github_release_assets(pv: PackageVersion) -> dict[str, str]:
    """{filename: download url} of the GitHub release that ships ``pv``."""
    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/",
        pv.wheel_url,
    )
    if not m:
        return {}
    owner, repo, tag = m.groups()
    try:
        release = _github_get_json(
            f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
        )
        return {
            a["name"]: a["browser_download_url"]
            for a in release.get("assets", [])
        }
    except Exception:  # noqa: BLE001 - e.g. rate limit
        return {}


def resolve_install_source(
    pkg: TaskPackage, version: str, requires_python: Optional[str] = None
) -> InstallSource:
    """
    How to install ``pkg==version`` here: for PyPI the version spec (the
    resolver picks a compatible wheel or the sdist); for GitHub a release
    wheel compatible with this platform (and only the Pythons it supports),
    else the release's sdist.
    """
    pv = pkg.get_version(version)
    pythons = candidate_pythons(requires_python or pv.requires_python)
    if pkg.kind == "pypi":
        return InstallSource([f'version = "=={version}"'], pythons, "PyPI")

    def _with_wheel(filename, url):
        ok = [py for py in pythons if wheel_is_compatible(filename, py)]
        if ok:
            return InstallSource([f"url = {json.dumps(url)}"], ok, filename)
        return None

    src = _with_wheel(pv.wheel_filename, pv.wheel_url)
    if src:
        return src
    assets = _github_release_assets(pv)
    for name, url in assets.items():
        if name.endswith(".whl") and name != pv.wheel_filename:
            try:
                if _wheel_name_version(name) != (pkg.pkg_name, version):
                    continue
            except IndexError:
                continue
            src = _with_wheel(name, url)
            if src:
                return src
    # The release's sdist: from the catalog (source_url) when it is the
    # release asset (not GitHub's tag archive, which hatch-vcs /
    # setuptools-scm packages can't be built from), else from the assets
    sdist_url = None
    if pv.source_url and not _is_tag_archive(pv.source_url):
        sdist_url = pv.source_url
    else:
        sdists = [n for n in assets if _is_sdist(n)]
        if sdists:
            name = next((n for n in sdists if n.endswith(".tar.gz")), sdists[0])
            sdist_url = assets[name]
        else:
            guess = _release_sdist_guess(pv)
            sdist_url = guess[0] if guess else None
    if sdist_url:
        name = os.path.basename(urlparse(sdist_url).path)
        return InstallSource(
            [f"url = {json.dumps(sdist_url)}"], pythons,
            f"{name} (source build: no wheel for this platform)",
        )
    raise RuntimeError(
        f"{pkg.pkg_name} {version}: no wheel for this platform "
        f"({pv.wheel_filename}) and no source distribution in its release"
    )


def write_pixi_manifest(
    pkg: TaskPackage,
    version: str,
    extras: list[str],
    requires_python: Optional[str],
    pins: Optional[list[str]] = None,
    python: Optional[str] = None,
    source: Optional[InstallSource] = None,
    system_requirements: Optional[dict] = None,
    log: Optional[Callable[[str], None]] = None,
    no_build: Optional[list[str]] = None,
    rich_platforms: Optional[bool] = None,
) -> Path:
    """Write a pixi.toml that pins exactly this package version.
    ``system_requirements`` (default: the host's) go on the platform entry
    (``rich_platforms``, default: when this pixi supports it), else into a
    ``[system-requirements]`` table."""
    if source is None:
        source = resolve_install_source(pkg, version, requires_python)
    py = python or source.pythons[0]

    dep_fields = list(source.dep_fields)
    if extras:
        dep_fields.append(f"extras = {_toml_list(extras)}")

    if system_requirements is None:
        system_requirements = (
            host_system_requirements(log) if DECLARE_SYSTEM_REQUIREMENTS
            else {}
        )
    if rich_platforms is None:
        rich_platforms = use_rich_platforms(pixi_version(find_pixi()))
    plat = current_pixi_platform()
    if rich_platforms:
        platforms, table = [host_platform_entry(plat, system_requirements)], ""
    else:
        platforms = [plat]
        table = system_requirements_toml(system_requirements)
    content = (
        "# Generated by napari-workflow-tasks\n"
        "[workspace]\n"
        f'name = "{pkg.pkg_name}-{version}"\n'
        'channels = ["conda-forge"]\n'
        f"platforms = {_toml_value(platforms)}\n"
        f"{table}"
        "\n[dependencies]\n"
        f'python = "{py}.*"\n'
        "\n[pypi-dependencies]\n"
        f'{pkg.pkg_name} = {{ {", ".join(dep_fields)} }}\n'
    )
    for name, spec in parse_pins(pins).items():
        if name != pkg.pkg_name:
            content += f"{name} = {json.dumps(spec)}\n"
    if no_build:
        # Use only wheels for these (their source build failed here)
        content += f"\n[pypi-options]\nno-build = {_toml_list(no_build)}\n"
    d = env_dir(pkg.pkg_name, version)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "pixi.toml"
    path.write_text(content)
    return path


def _run_streaming(
    cmd: list[str], log: Callable[[str], None], env: Optional[dict] = None
) -> str:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        lines.append(line)
        log(line)
    if proc.wait() != 0:
        tail = "\n".join(lines[-20:])
        err = RuntimeError(f"Command failed: {' '.join(cmd)}\n{tail}")
        err.output = "\n".join(lines)  # full output, for diagnosis
        raise err
    return "\n".join(lines)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def failed_builds(output: str) -> list[str]:
    """Packages whose source build failed ("Failed to build `x==1.0`")."""
    names = re.findall(
        r"Failed to build `([A-Za-z0-9._-]+)(?:\[[^`]*\])?(?:==|@| |`)",
        _ANSI.sub("", output),
    )
    return list(dict.fromkeys(normalize_package_name(n) for n in names))


def _install_with_no_build_retry(
    install: Callable[[list[str]], None],
    pkg_name: str,
    no_build: list[str],
    log: Callable[[str], None],
) -> None:
    """Run ``install(no_build)``. When it fails because building a
    dependency from source failed ("Failed to build `X==v`"), add X to
    ``no_build`` (in place) and retry, so that the resolver picks the newest
    release of X with a usable wheel. The package names always come from
    the error output. Re-raises the last error when nothing new to add."""
    while True:
        try:
            install(no_build)
            return
        except RuntimeError as e:
            new = [
                n for n in failed_builds(getattr(e, "output", str(e)))
                if n not in no_build and n != pkg_name
            ]
            if not new:
                raise
            no_build.extend(new)
            log(f"Building {', '.join(new)} from source failed; "
                f"retrying with wheels only for {', '.join(new)}")


# Don't download huge source archives just to look for a pixi.lock
MAX_SOURCE_DOWNLOAD = 50 * 1024 * 1024


def _safe_extract(archive: bytes, dest: Path) -> Path:
    """Extract a .tar.gz (no paths outside ``dest``, no links); returns the
    project root (the single top-level folder of sdists / GitHub tarballs)."""
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            target = (root / m.name).resolve()
            if target != root and not str(target).startswith(str(root) + os.sep):
                raise ValueError(f"Unsafe path in archive: {m.name}")
            if m.issym() or m.islnk() or m.isdev():
                continue
            members.append(m)
        if hasattr(tarfile, "data_filter"):  # Python >= 3.12 (and backports)
            tar.extractall(root, members=members, filter="data")
        else:
            tar.extractall(root, members=members)
    tops = [p for p in root.iterdir() if p.is_dir()]
    return tops[0] if len(tops) == 1 else root


def _pixi_manifest_in(project: Path) -> Optional[Path]:
    """pixi.toml, or pyproject.toml with a [tool.pixi] table."""
    if (project / "pixi.toml").exists():
        return project / "pixi.toml"
    pyproject = project / "pyproject.toml"
    if pyproject.exists() and "[tool.pixi" in pyproject.read_text(errors="ignore"):
        return pyproject
    return None


def _yaml_scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text


def lock_platform_names(lock_text: str, environment: str = "default") -> dict:
    """{platform name: subdir} locked for one environment of a pixi.lock
    (v4-v7 layout: ``environments: <env>: packages: <name>:``).

    Since pixi 0.71 a lock is keyed by the *names* of the manifest's
    platform entries, listed with their subdir in a top-level
    ``platforms:`` section (e.g. ``osx-arm64-host`` or
    ``osx-arm64-macos-14-0`` -> ``osx-arm64``; also when the manifest uses
    the ``[system-requirements]`` table). Older locks are keyed by the
    subdir itself."""
    subdirs: dict = {}
    names: list = []
    section = cur_plat = cur_env = None
    in_packages = False
    for line in lock_text.splitlines()[1:]:
        if line and not line[0].isspace() and not line.startswith("-"):
            section = line.split(":", 1)[0].strip()
            if section == "packages":
                break
            continue
        if section == "platforms":
            m = re.match(r"^- name:\s*(.+?)\s*$", line)
            if m:
                cur_plat = _yaml_scalar(m.group(1))
                subdirs[cur_plat] = cur_plat
                continue
            m = re.match(r"^  subdir:\s*(.+?)\s*$", line)
            if m and cur_plat is not None:
                subdirs[cur_plat] = _yaml_scalar(m.group(1))
            continue
        if section != "environments":
            continue
        m = re.match(r"^  (\S[^:]*):\s*$", line)
        if m:
            cur_env, in_packages = _yaml_scalar(m.group(1)), False
            continue
        if re.match(r"^    packages:\s*$", line):
            in_packages = True
            continue
        if re.match(r"^    \S", line):
            in_packages = False
            continue
        m = re.match(r"^      (\S+):\s*$", line)
        if m and in_packages and cur_env == environment:
            names.append(_yaml_scalar(m.group(1)))
    return {n: subdirs.get(n, n) for n in names}


def lock_platforms(lock_text: str, environment: str = "default") -> list[str]:
    """Platforms (subdirs, e.g. "osx-arm64") locked for one environment of
    a pixi.lock, whatever the names of its platform entries."""
    return list(dict.fromkeys(lock_platform_names(lock_text, environment).values()))


def _pixi_table_prefix(manifest: Path) -> str:
    """Table prefix of pixi's config in ``manifest`` ("tool.pixi." in a
    pyproject.toml, "" in a pixi.toml)."""
    return "tool.pixi." if manifest.name == "pyproject.toml" else ""


def _load_toml(text: str) -> dict:
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        import tomli as tomllib
    return tomllib.loads(text)


def _pixi_section(doc: dict, manifest: Path) -> Optional[dict]:
    if manifest.name == "pyproject.toml":
        return doc.get("tool", {}).get("pixi")
    return doc


def _pixi_config(manifest: Path) -> Optional[dict]:
    """pixi's config in ``manifest`` (pixi.toml, or [tool.pixi] of a
    pyproject.toml); None if missing/unreadable."""
    if not manifest.exists():
        return None
    try:
        data = _load_toml(manifest.read_text())
    except ValueError:
        return None
    return _pixi_section(data, manifest)


def _sysreq_version(value) -> Optional[str]:
    if isinstance(value, dict):  # e.g. libc = { family = "glibc", version = "2.28" }
        value = value.get("version")
    return None if value is None else str(value)


def merge_system_requirements(own: dict, host: dict) -> tuple[dict, list[str]]:
    """Package's own system requirements completed with the host's.
    Returns (merged, problems); a problem = the package needs more than the
    host has (the env could not run here)."""
    merged, problems = dict(own), []
    for key, host_v in host.items():
        own_v = _sysreq_version(own.get(key))
        if own_v is None:
            merged[key] = host_v
            continue
        host_ver = _sysreq_version(host_v)
        try:
            if host_ver is not None and Version(own_v) > Version(host_ver):
                problems.append(f"{key} {own_v} required, host has {host_ver}")
        except InvalidVersion:
            pass
    return merged, problems


# --- Editing a package's pixi config (path (b)) ---------------------------
# Plain text edits (comments and layout elsewhere are kept), each verified
# by re-parsing the whole file: anything but the intended change raises
# RuntimeError and leaves the file unchanged.

def _table_span(text: str, header: str) -> Optional[tuple[int, int, int]]:
    """(start of the ``[header]`` line, start of its body, end of the
    table) in ``text``; None if there is no such table header."""
    m = re.search(r"^\[\s*" + re.escape(header).replace(r"\.", r"\s*\.\s*")
                  + r"\s*\][^\n]*(?:\n|$)", text, re.M)
    if not m:
        return None
    nxt = re.search(r"^\[", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return m.start(), m.end(), end


def _value_end(text: str, i: int) -> int:
    """End of the TOML value starting at ``text[i]`` (arrays and inline
    tables may span lines and contain strings / comments)."""
    if text[i] not in "[{":
        j = text.find("\n", i)
        return len(text) if j < 0 else j
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c in "\"'":
            q = text[j:j + 3] if text[j:j + 3] in ('"""', "'''") else c
            k = j + len(q)
            while k < len(text):
                if q[0] == '"' and text[k] == "\\":
                    k += 2
                    continue
                if text.startswith(q, k):
                    break
                k += 1
            j = k + len(q)
            continue
        if c == "#":
            nl = text.find("\n", j)
            j = len(text) if nl < 0 else nl
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise ValueError("unterminated value")


def _set_table_key(text: str, header: str, key: str, value) -> str:
    """``key = value`` in table ``[header]`` (replacing its value)."""
    span = _table_span(text, header)
    if span is None:
        raise RuntimeError(f"no [{header}] table")
    _, body, end = span
    m = re.search(r"^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*",
                  text[body:end], re.M)
    if m is None:
        insert = text[body:end].rstrip("\n")
        return (text[:body] + insert + ("\n" if insert else "")
                + f"{key} = {_toml_value(value)}\n" + text[body + len(insert):])
    start = body + m.end()
    stop = _value_end(text, start)
    return text[:start] + _toml_value(value) + text[stop:]


def _remove_table(text: str, header: str) -> str:
    span = _table_span(text, header)
    if span is None:
        return text
    start, _, end = span
    return text[:start] + text[end:]


def _write_verified(manifest: Path, original: str, text: str,
                    expected: dict, what: str) -> None:
    """Write ``text`` to ``manifest`` if it parses to ``expected``."""
    try:
        parsed = _load_toml(text)
    except ValueError as e:
        raise RuntimeError(f"Could not set {what} in {manifest.name}: {e}") from e
    if parsed != expected:
        raise RuntimeError(f"Could not set {what} in {manifest.name}")
    if text != original:
        manifest.write_text(text)


def _roundtrip(value):
    """``value`` as it reads back from TOML (numbers become strings)."""
    return _load_toml("x = " + _toml_value(value))["x"]


def _replace_pixi_table(manifest: Path, table: str, data: dict) -> None:
    """Replace (or add) pixi's ``[<table>]`` (``[tool.pixi.<table>]`` in a
    pyproject.toml) with ``data``. The result is re-parsed; on any mismatch
    the file is left unchanged and RuntimeError is raised."""
    import copy

    prefix = _pixi_table_prefix(manifest)
    header = prefix + table
    original = manifest.read_text()
    text = _remove_table(original, header)
    body = "".join(f"{k} = {_toml_value(v)}\n" for k, v in data.items())
    text = text.rstrip("\n") + f"\n\n[{header}]\n" + body
    try:
        expected = copy.deepcopy(_load_toml(original))
    except ValueError as e:
        raise RuntimeError(f"Could not read {manifest.name}: {e}") from e
    node = expected
    for part in header.split(".")[:-1]:
        node = node.setdefault(part, {})
    node[header.split(".")[-1]] = {k: _roundtrip(v) for k, v in data.items()}
    _write_verified(manifest, original, text, expected, f"[{header}]")


def write_system_requirements(manifest: Path, reqs: dict) -> None:
    """Replace pixi's ``system-requirements`` table of ``manifest`` (pixi
    without rich platforms; ``reqs`` as from host_system_requirements)."""
    _replace_pixi_table(manifest, "system-requirements", sysreqs_to_table(reqs))


def _workspace_key(cfg: dict) -> str:
    for key in ("workspace", "project"):
        if "platforms" in (cfg.get(key) or {}):
            return key
    return "workspace"


def _platform_of(entry) -> Optional[str]:
    return entry.get("platform") if isinstance(entry, dict) else entry


def pixi_config_sysreqs(cfg: dict, platform_: str) -> dict:
    """System requirements the package's pixi config declares for
    ``platform_``, in either syntax: the (deprecated)
    ``[system-requirements]`` table and/or its ``platforms`` entry."""
    own = sysreqs_for_platform(
        sysreqs_from_table(cfg.get("system-requirements") or {}), platform_)
    for e in (cfg.get(_workspace_key(cfg)) or {}).get("platforms") or []:
        if isinstance(e, dict) and e.get("platform") == platform_:
            own.update({k: v for k, v in e.items() if k in _SYSREQ_OS})
            break
    return own


def write_platform_requirements(
    manifest: Path, platform_: str, reqs: dict
) -> Optional[str]:
    """Put ``reqs`` on the ``platform_`` entry of the package's
    ``platforms`` list ([tool.pixi.workspace] / [workspace] / [project]),
    as a named table (``{ name = "osx-arm64-host", platform = "osx-arm64",
    macos = "26.7.1" }``); the other entries are kept. pixi rejects the
    ``[system-requirements]`` table next to such entries, so a table the
    package declares is removed and its values moved to the entries they
    apply to. Feature ``platforms`` lists follow the renamed entries.
    Returns the name of the host's entry (None: the bare platform)."""
    import copy

    original = manifest.read_text()
    doc = _load_toml(original)
    cfg = _pixi_section(doc, manifest)
    if cfg is None:
        raise RuntimeError(f"no pixi config in {manifest.name}")
    expected = copy.deepcopy(doc)
    exp_cfg = _pixi_section(expected, manifest)
    prefix = _pixi_table_prefix(manifest)
    wkey = _workspace_key(cfg)
    table = sysreqs_from_table(cfg.get("system-requirements") or {})
    rename: dict = {}
    entries, host_name, done = [], None, False
    for e in cfg[wkey]["platforms"]:
        p = _platform_of(e)
        if p == platform_ and not done:
            done = True
            keep = {k: v for k, v in e.items()
                    if k not in ("name", "platform") and k not in _SYSREQ_OS
                    } if isinstance(e, dict) else {}
            new = host_platform_entry(
                platform_, reqs, e.get("name") if isinstance(e, dict) else None)
            if keep:
                if not isinstance(new, dict):
                    new = {"name": platform_ + HOST_PLATFORM_SUFFIX,
                           "platform": platform_}
                new.update(keep)
            if isinstance(new, dict):
                host_name = new["name"]
                if not isinstance(e, dict):
                    rename[e] = host_name
            entries.append(new)
        elif table and isinstance(e, str) and sysreqs_for_platform(table, e):
            # the package's own [system-requirements], for its other platforms
            new = host_platform_entry(e, table, e + "-sysreq")
            rename[e] = new["name"]
            entries.append(new)
        else:
            entries.append(e)
    if not done:
        raise RuntimeError(f"{platform_} is not in the package's platforms")
    text = _set_table_key(original, prefix + wkey, "platforms", entries)
    exp_cfg[wkey]["platforms"] = _roundtrip(entries)
    if "system-requirements" in cfg:
        text = _remove_table(text, prefix + "system-requirements")
        del exp_cfg["system-requirements"]
    for fname, feat in (cfg.get("feature") or {}).items():
        plats = (feat or {}).get("platforms")
        if not plats or not any(p in rename for p in plats):
            continue
        new_plats = [rename.get(p, p) for p in plats]
        text = _set_table_key(text, f"{prefix}feature.{fname}", "platforms",
                              new_plats)
        exp_cfg["feature"][fname]["platforms"] = new_plats
    _write_verified(manifest, original, text, expected, "the platforms")
    return host_name


def _conda_spec(spec: str) -> str:
    """PEP 440 specifier -> conda MatchSpec version (``~=1.4`` ->
    ``>=1.4,1.*``); other operators are the same."""
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if part.startswith("~="):
            v = part[2:].strip()
            head = ".".join(v.split(".")[:-1]) or v
            out += [f">={v}", f"{head}.*"]
        elif part.startswith("==="):
            out.append("==" + part[3:].strip())
        else:
            out.append(part)
    return ",".join(out) or "*"


def _and_spec(current, spec: str, name: str):
    """``current`` dependency spec (str or table) AND ``spec``."""
    def _and(cur: str) -> str:
        cur = (cur or "").strip()
        if cur in ("", "*"):
            return spec
        return cur if spec == "*" else f"{cur},{spec}"

    if current is None:
        return spec
    if isinstance(current, str):
        return _and(current)
    if isinstance(current, dict):
        source = [k for k in ("path", "git", "url") if k in current]
        if source:
            raise RuntimeError(
                f"can't pin {name}: the package's pixi config installs it "
                f"from a {source[0]}")
        return {**current, "version": _and(current.get("version", ""))}
    raise RuntimeError(f"can't pin {name}: unexpected spec {current!r}")


def write_pins_to_pixi_config(
    manifest: Path, pins: list[str], kinds: dict, pkg_name: str
) -> list[str]:
    """Add the pinned packages to the package's pixi config: conda
    ``dependencies`` for packages that come from conda in its environment
    (``kinds``: {name: "conda" | "pypi"}, from its pixi.lock), else
    ``pypi-dependencies``; ANDed with the package's own spec for them.
    Returns "pin -> table" descriptions."""
    cfg = _pixi_config(manifest) or {}
    prefix = _pixi_table_prefix(manifest)
    tables = {t: dict(cfg.get(t) or {})
              for t in ("dependencies", "pypi-dependencies")}
    keys = {t: {normalize_package_name(k): k for k in d}
            for t, d in tables.items()}
    changed, applied = set(), []
    for name, spec in parse_pins(pins).items():
        if name == pkg_name:
            continue  # the release itself
        kind = kinds.get(name) or (
            "conda" if name in keys["dependencies"]
            and name not in keys["pypi-dependencies"] else "pypi")
        table = "dependencies" if kind == "conda" else "pypi-dependencies"
        value = _conda_spec(spec) if kind == "conda" else spec
        key = keys[table].get(name, name)
        tables[table][key] = _and_spec(tables[table].get(key), value, name)
        changed.add(table)
        applied.append(f"{key} {value} -> [{prefix}{table}]")
    for table in sorted(changed):
        _replace_pixi_table(manifest, table, tables[table])
    return applied


def write_pypi_no_build(manifest: Path, names: list[str]) -> list[str]:
    """Add ``names`` to pixi's ``[pypi-options] no-build`` of ``manifest``
    (keeping the other pypi-options). Returns the resulting no-build list."""
    cfg = _pixi_config(manifest) or {}
    opts = dict(cfg.get("pypi-options") or {})
    current = opts.get("no-build")
    if current is True:
        raise RuntimeError("source builds are already disabled for all packages")
    merged = list(dict.fromkeys([*(current or []), *names]))
    opts["no-build"] = merged
    _replace_pixi_table(manifest, "pypi-options", opts)
    return merged


_CHECK_SCRIPT = r"""
import importlib, importlib.util, sys, traceback
module, exes = sys.argv[1], sys.argv[2:]
s = importlib.util.find_spec(module)
if s is None or not s.submodule_search_locations:
    print("CHECKFAIL=" + module + ": module not found"); sys.exit(3)
print("PKGDIR=" + list(s.submodule_search_locations)[0], flush=True)
import os, re
pkgdir = list(s.submodule_search_locations)[0]
names, wrappers = [], []
for exe in exes:
    names.append(module + "." + exe[:-3].replace("/", ".") if exe.endswith(".py") else exe)
    # `from X import run_fractal_task` usually sits in `if __name__ == "__main__":`
    try:
        src = open(os.path.join(pkgdir, exe), encoding="utf-8").read()
    except OSError:
        continue
    for w in re.findall(r"^\s*from\s+([\w.]+)\s+import\s+run_fractal_task", src, re.M):
        if w not in wrappers:
            wrappers.append(w)
bad = 0
for name in names + wrappers:
    try:
        importlib.import_module(name)
        print("IMPORTOK=" + name, flush=True)
    except BaseException as e:
        bad += 1
        tb = traceback.format_exception(type(e), e, e.__traceback__)
        print("".join(tb[-6:]), flush=True)
        print("CHECKFAIL=" + name + ": " + type(e).__name__ + ": " + str(e), flush=True)
sys.exit(4 if bad else 0)
"""


def _task_executables(info: VersionInfo) -> list[str]:
    exes = []
    for t in info.task_list:
        for key in ("executable_non_parallel", "executable_parallel"):
            exe = t.get(key)
            if exe and exe not in exes:
                exes.append(exe)
    return exes


def _check_modules(info: VersionInfo) -> list[str]:
    """Arguments of the import check: the package module + task files."""
    return [info.module_name, *_task_executables(info)]


def check_env(
    pixi: str,
    manifest: Path,
    info: VersionInfo,
    log: Callable[[str], None],
    frozen: bool = False,
) -> str:
    """Locate the package in the env and import every task module, the same
    way tasks are run (``pixi run``, with ``--frozen`` for an env installed
    from the package's pixi.lock; see _widget._execute_task).
    Returns the package dir; raises RuntimeError with the import error."""
    cmd = [
        pixi, "run", *(["--frozen"] if frozen else []),
        "--manifest-path", str(manifest),
        "python", "-c", _CHECK_SCRIPT, *_check_modules(info),
    ]
    try:
        out, returncode = _run_streaming(cmd, log=lambda _line: None), 0
    except RuntimeError as e:
        out, returncode = getattr(e, "output", str(e)), 1
    lines = out.splitlines()
    fails = [ln[len("CHECKFAIL="):] for ln in lines if ln.startswith("CHECKFAIL=")]
    pkg_dir = next(
        (ln[len("PKGDIR="):] for ln in lines if ln.startswith("PKGDIR=")), None
    )
    if returncode != 0 or pkg_dir is None:
        by_error: dict[str, list[str]] = {}
        for f in fails:
            mod, _, err = f.partition(": ")
            by_error.setdefault(err, []).append(mod)
        detail = "\n".join(
            f"{err}\n  in {len(mods)} module(s): {', '.join(mods[:3])}"
            + (", ..." if len(mods) > 3 else "")
            for err, mods in by_error.items()
        ) or "\n".join(lines[-20:])
        for ln in lines[-40:]:
            log(ln)
        raise RuntimeError(
            f"The environment was installed but the tasks can't be imported:\n{detail}"
        )
    n_ok = sum(ln.startswith("IMPORTOK=") for ln in lines)
    log(f"Import check OK ({n_ok} task modules)")
    return pkg_dir


def _locked_versions(pixi: str, manifest: Path) -> dict[str, str]:
    out = subprocess.run(
        [pixi, "list", "--frozen", "--json", "--manifest-path", str(manifest)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        return {
            normalize_package_name(p["name"]): p["version"]
            for p in json.loads(out.stdout)
        }
    except (ValueError, KeyError, TypeError):
        return {}


def lock_package_versions(
    pixi: str, manifest: Path, platform_name: str, environment: str = "default"
) -> Optional[dict[str, tuple[str, str]]]:
    """{name: (version, "conda" | "pypi")} locked in the pixi.lock next to
    ``manifest`` for one environment and platform (the lock's platform
    *name*, see lock_platform_names), read by ``pixi list --frozen``
    (nothing is installed or changed). None if pixi can't tell."""
    try:
        out = subprocess.run(
            [pixi, "list", "--frozen", "--json", "--environment", environment,
             "--platform", platform_name, "--manifest-path", str(manifest)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=300,
        )
        rows = json.loads(out.stdout)
        if out.returncode != 0 or not isinstance(rows, list) or not rows:
            return None  # e.g. a name that is not this platform's entry
        return {
            normalize_package_name(p["name"]): (str(p["version"]), p.get("kind") or "")
            for p in rows
        }
    except (OSError, subprocess.SubprocessError, ValueError, KeyError,
            TypeError):
        return None


@dataclass
class LockPinCheck:
    """Pins checked against a release's pixi.lock (before installing)."""

    checked: bool  # False: could not read the lock's versions here
    unmet: list  # e.g. ["numpy<2 (lock has 2.4.3)"]
    versions: dict  # {pinned name: locked version}
    kinds: dict  # {name: "conda" | "pypi"} of every locked package


def check_pins_against_lock(
    pixi: str, manifest: Path, lock_text: str, platform_: str,
    pins: list[str],
) -> LockPinCheck:
    """Whether the release's pixi.lock (default environment, every
    platform entry for ``platform_``) satisfies ``pins``. ``kinds`` also
    comes from another platform when the lock does not cover this one.

    ``pixi list --platform`` takes the *manifest's* entry names; locks of
    pixi 0.71-0.77 key them by aliases (``p1``), newer ones by the names."""
    names = lock_platform_names(lock_text)
    cfg = _pixi_config(manifest) or {}
    declared = {}
    for e in (cfg.get(_workspace_key(cfg)) or {}).get("platforms") or []:
        if isinstance(e, str):
            declared[e] = e
        elif isinstance(e, dict) and e.get("name") and e.get("platform"):
            declared[e["name"]] = e["platform"]
    if platform_ not in names.values():
        here = []  # the lock does not cover this platform
    else:
        here = list(dict.fromkeys(
            [n for n, sub in declared.items() if sub == platform_]
            + [n for n, sub in names.items() if sub == platform_]))
    others = [n for n in dict.fromkeys([*declared, *names]) if n not in here]
    unmet, versions, kinds, checked = [], {}, {}, bool(here)
    got_here = False
    for name in here + others:
        locked = lock_package_versions(pixi, manifest, name)
        if locked is None:
            continue
        got_here = got_here or name in here
        for pkg, (_v, kind) in locked.items():
            kinds.setdefault(pkg, kind)
        if name not in here:
            if kinds:
                break
            continue
        flat = {k: v for k, (v, _kind) in locked.items()}
        for bad in unmet_pins(pins, flat, where="lock"):
            if bad not in unmet:
                unmet.append(bad)
        for pkg in parse_pins(pins):
            if pkg in flat:
                versions[pkg] = flat[pkg]
    return LockPinCheck(checked and got_here, unmet, versions, kinds)


def unmet_pins(
    pins: Optional[list[str]], versions: dict[str, str], where: str = "env"
) -> list[str]:
    """Pins that the given {name: version} don't satisfy (a package that is
    not in the env is not constrained)."""
    bad = []
    for pin in pins or []:
        req = Requirement(pin)
        v = versions.get(normalize_package_name(req.name))
        if v is None:
            continue  # not in the env: nothing to constrain
        try:
            if not req.specifier.contains(v, prereleases=True):
                bad.append(f"{pin} ({where} has {v})")
        except InvalidVersion:
            bad.append(f"{pin} ({where} has {v})")
    return bad


# --------------------------------------------------------------------------
# Install caches (see _env_cache): source archives, native libraries,
# install recipes, import-check records
# --------------------------------------------------------------------------

INCOMPLETE_MARKER = ".install_incomplete"
SOURCE_SUBDIR = "source"  # source archive -> env_dir(pkg, ver)/source/<top>/

# Kinds of environments (entry["env_kind"])
ENV_LOCKED = "pixi.lock"  # (a) the package's own pixi.lock, --frozen
ENV_PIXI_CONFIG = "pixi config"  # (b) its pixi config, re-solved here
ENV_RESOLVED = "resolved"  # (c) generated pixi.toml with the wheel


def cache_dir() -> Path:
    """Root of the install caches (default: next to ENVS_DIR)."""
    return Path(CACHE_DIR) if CACHE_DIR is not None else ENVS_DIR.parent / "cache"


def native_store_dir() -> Path:
    return cache_dir() / "native"


def recipes_dir() -> Path:
    return cache_dir() / "recipes"


def _fetch_source(url: str, log: Callable[[str], None]) -> bytes:
    """The source archive, from the local cache or downloaded into it
    (releases are immutable)."""
    name = os.path.basename(urlparse(url).path) or "source.tar.gz"
    d = cache_dir() / "sources" / _env_cache.text_sha256(url)[:16]
    archive = d / name
    if archive.exists():
        log(f"Using the cached {name}")
        return archive.read_bytes()
    data = _http_get(url)
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.{os.getpid()}.part"
        tmp.write_bytes(data)
        os.replace(tmp, archive)
    except OSError as e:
        log(f"Could not cache the source archive: {e}")
    return data


def _extract_source(archive: bytes, src_dir: Path) -> Path:
    """Extract the source archive into ``src_dir`` (upstream layout
    ``source/<top>/``), keeping an existing ``.pixi`` env of the same
    project: a re-install re-uses it instead of re-creating every file."""
    keep = src_dir.parent / ".pixi-source-env"
    shutil.rmtree(keep, ignore_errors=True)
    kept_top = None
    if src_dir.exists():
        envs = sorted(src_dir.glob("*/.pixi"))
        if envs:
            kept_top = envs[0].parent.name
            envs[0].rename(keep)
        shutil.rmtree(src_dir)
    try:
        project = _safe_extract(archive, src_dir)
    finally:
        if keep.exists():
            target = src_dir / kept_top / ".pixi" if kept_top else None
            if target is not None and target.parent.is_dir() and not target.exists():
                keep.rename(target)
            else:
                shutil.rmtree(keep, ignore_errors=True)
    return project


def _is_tag_archive(url: Optional[str]) -> bool:
    return bool(url) and "/archive/refs/tags/" in url


def _release_sdist_guess(pv: PackageVersion) -> Optional[tuple[str, Optional[int]]]:
    """(url, size) of the sdist asset of the GitHub release that ships
    ``pv``'s wheel, guessed from the wheel URL and checked with one HEAD
    request (no API call). For catalogs whose source_url is still the tag
    archive."""
    m = re.match(
        r"(https://github\.com/[^/]+/[^/]+/releases/download/[^/]+)/",
        pv.wheel_url,
    )
    if not m:
        return None
    url = f"{m.group(1)}/{pv.wheel_filename.split('-')[0]}-{pv.version}.tar.gz"
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            size = res.headers.get("Content-Length")
            return url, int(size) if size and size.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def _source_build_env(pkg: TaskPackage, version: str) -> dict:
    """Environment for building the package from GitHub's tag archive: it
    has no PKG-INFO and no .git, so hatch-vcs / setuptools-scm can't tell
    the version without being told."""
    env = dict(os.environ)
    env["SETUPTOOLS_SCM_PRETEND_VERSION"] = version
    key = re.sub(r"[^A-Z0-9]+", "_", pkg.pkg_name.upper())
    env[f"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_{key}"] = version
    return env


@dataclass
class _Source:
    """The extracted source of a release that ships a pixi.lock."""

    project: Path
    manifest: Path
    url: str
    env: Optional[dict] = None  # build env for the tag archive


def _prepare_source(
    pkg: TaskPackage, version: str, log: Callable[[str], None]
) -> Optional[_Source]:
    """Download (cached) and extract the release's source archive into
    ``env_dir/source``. Returns it if it ships a pixi.lock with a pixi
    manifest, else None (and nothing is left in ``source``)."""
    pv = pkg.get_version(version)
    if not pv.source_url or has_pixi_lock(pv) is False:
        return None
    url, size, env = pv.source_url, pv.source_size, None
    if _is_tag_archive(url):
        # GitHub's tag archive can't be built by hatch-vcs/setuptools-scm
        # packages (no PKG-INFO): prefer the release's sdist asset
        asset = _release_sdist_guess(pv)
        if asset:
            url, size = asset
        else:
            env = _source_build_env(pkg, version)
    if size and size > MAX_SOURCE_DOWNLOAD:
        log(f"Source archive too large to check for pixi.lock ({size >> 20} MB)")
        return None

    log(f"Looking for pixi.lock in {url}")
    try:
        archive = _fetch_source(url, log)
    except Exception as e:  # noqa: BLE001
        log(f"Could not download the source archive: {e}")
        return None
    if len(archive) > MAX_SOURCE_DOWNLOAD:
        log(f"Source archive too large ({len(archive) >> 20} MB)")
        return None

    src_dir = env_dir(pkg.pkg_name, version) / SOURCE_SUBDIR
    project = _extract_source(archive, src_dir)
    manifest = _pixi_manifest_in(project)
    if manifest is None or not (project / "pixi.lock").exists():
        log("No pixi.lock in this release")
        shutil.rmtree(src_dir, ignore_errors=True)
        return None
    return _Source(project, manifest, url, env)


def _pins_text(pins) -> str:
    return ", ".join(pins)


def _finish_install(
    pkg: TaskPackage,
    version: str,
    info: VersionInfo,
    pixi: str,
    manifest: Path,
    kind: str,
    log: Callable[[str], None],
    pins: Optional[list[str]] = None,
    recipe: Optional[dict] = None,
    pins_how: Optional[str] = None,
) -> tuple[dict, dict]:
    """After a successful ``pixi install``: check pins (when given), share
    native libraries with the store, then run the import check unless it
    already passed for this exact env. Returns (entry, check record).
    ``entry["pins_applied"]``: how the pins were applied (``pins_how``),
    and the installed versions of the pinned packages."""
    frozen = kind == ENV_LOCKED
    pins_applied = None
    if pins:
        versions = _locked_versions(pixi, manifest)
        bad = unmet_pins(pins, versions)
        if bad:
            raise RuntimeError("pins not satisfied: " + ", ".join(bad))
        pinned = {n: versions.get(n) for n in parse_pins(pins)}
        if not versions:
            log("Warning: could not list the installed packages to verify "
                f"the pinned packages ({_pins_text(pins)})")
        log(f"Pinned packages ({pins_how or kind}): " + ", ".join(
            f"{n}=={v}" if v else f"{n} (not in the env)"
            for n, v in pinned.items()))
        pins_applied = {"pins": list(pins), "how": pins_how or kind,
                        "env_kind": kind, "versions": pinned,
                        "verified": bool(versions)}
    envs = manifest.parent / ".pixi" / "envs"
    stats = _env_cache.dedup_tree(envs, native_store_dir(), log)
    log(stats.summary())

    prefix = envs / "default"
    lock = manifest.parent / "pixi.lock"
    expected = {
        "lock_sha256": _env_cache.file_sha256(lock) if lock.exists() else None,
        "modules": _check_modules(info),
        "env_fingerprint": _env_cache.env_fingerprint(prefix),
    }
    marker = _env_cache.load_check(prefix)
    recipe_check = (recipe or {}).get("check")
    pkg_dir = None
    if (
        expected["lock_sha256"]
        and _env_cache.check_matches(marker, expected)
        and (recipe is None or _env_cache.check_matches(
            recipe_check,
            {k: expected[k] for k in ("lock_sha256", "modules")},
        ))
        and os.path.isdir(marker.get("pkg_dir", ""))
    ):
        pkg_dir = marker["pkg_dir"]
        log("Import check skipped: it passed for this exact environment")
    if pkg_dir is None:
        _env_cache.clear_check(prefix)
        t0 = time.monotonic()
        pkg_dir = check_env(pixi, manifest, info, log, frozen=frozen)
        log(f"Import check took {time.monotonic() - t0:.1f}s")
        record = {**expected, "ok": True, "pkg_dir": pkg_dir,
                  "seconds": round(time.monotonic() - t0, 1)}
        _env_cache.save_check(prefix, record)
    else:
        record = {**marker, **expected}
    log(
        f"Installed {pkg.pkg_name}=={version} at {pkg_dir}"
        + (" (from the package's pixi.lock)" if frozen else f" ({kind})")
    )
    entry = {
        "src_path": pkg_dir,
        "manifest_path": os.path.join(pkg_dir, "__FRACTAL_MANIFEST__.json"),
        "pixi_manifest_path": str(manifest),
        # Run with `pixi run --frozen` (see _widget._execute_task)
        "pixi_frozen": frozen,
        "env_kind": kind,
        "version": version,
        "pins_applied": pins_applied,
    }
    return entry, record


def _make_recipe(
    pkg: TaskPackage, version: str, kind: str, manifest: Path, **extra
) -> Optional[dict]:
    lock = manifest.parent / "pixi.lock"
    if not lock.exists() or not manifest.exists():
        return None
    return {
        "env_kind": kind,
        "manifest_relpath": manifest.resolve().relative_to(
            env_dir(pkg.pkg_name, version).resolve()
        ).as_posix(),
        "manifest_text": manifest.read_text(),
        "lock_text": lock.read_text(),
        **extra,
    }


def _run_install(cmd: list[str], log, env: Optional[dict] = None) -> str:
    # ``env`` only when needed (keeps _run_streaming's (cmd, log) call shape)
    if env is None:
        return _run_streaming(cmd, log)
    return _run_streaming(cmd, log, env=env)


def _install_from_source(
    pkg, version, info, src: _Source, pixi: str, log, extras, pins,
    sysreqs: dict, rich: bool = True,
) -> Optional[tuple[dict, dict]]:
    """(a) the release's own pixi.lock (``pixi install --frozen``) when it
    satisfies the pins, else (b) a re-solve of its pixi config with the
    host's system requirements and the pins. None = use the resolved
    install (c)."""
    manifest, project = src.manifest, src.project
    lock = project / "pixi.lock"
    plat = current_pixi_platform()
    lock_text = lock.read_text(errors="ignore")

    def _done(kind: str, check_pins, how=None, **extra) -> tuple[dict, dict]:
        entry, check = _finish_install(
            pkg, version, info, pixi, manifest, kind, log, pins=check_pins,
            pins_how=how,
        )
        recipe = _make_recipe(
            pkg, version, kind, manifest, source_url=src.url,
            build_env=bool(src.env), check=check,
            pins_applied=entry.get("pins_applied"), **extra,
        )
        return entry, recipe

    # Pinned versions win: check them against the lock first (no install)
    lock_check = None
    if pins:
        lock_check = check_pins_against_lock(pixi, manifest, lock_text, plat, pins)
    use_a = plat in lock_platforms(lock_text)
    if not use_a:
        log(f"The pixi.lock of this release does not cover {plat}")
    elif lock_check is not None and lock_check.unmet:
        use_a = False
        log("The pixi.lock of this release does not satisfy the pinned "
            "packages: " + ", ".join(lock_check.unmet)
            + "; re-solving its dependencies with the pins instead")
    elif lock_check is not None:
        if lock_check.checked:
            log("Pinned packages satisfied by the pixi.lock: " + ", ".join(
                f"{p} (lock has {lock_check.versions[n]})"
                if n in lock_check.versions else f"{p} (not in the lock)"
                for p, n in zip(pins, parse_pins(pins))))
        else:
            log("Could not read the pixi.lock's versions; the pinned "
                "packages are checked after installing it")

    # (a) the package's own lock file, as-is. Extras are not used: the
    # authors locked and tested exactly this environment.
    if use_a:
        log(f"Installing {pkg.pkg_name}=={version} from its pixi.lock (exact versions)")
        if extras:
            log("Extras are not used for the pixi.lock install (only if it "
                "falls back to resolving dependencies)")
        lock_sha = _env_cache.file_sha256(lock)
        try:
            _run_install(
                [pixi, "install", "--frozen", "--manifest-path", str(manifest)],
                log, src.env,
            )
            return _done(ENV_LOCKED, pins or None,
                         how="satisfied by the package's pixi.lock")
        except RuntimeError as e:
            log("Installing from pixi.lock failed, falling back: "
                + (str(e).splitlines() or [""])[-1])
        finally:
            if lock.exists() and _env_cache.file_sha256(lock) != lock_sha:
                log("Warning: the package's pixi.lock was changed by pixi")

    # (b) re-solve the package's own pixi config (lock unusable here)
    if extras:
        log("Extras requested: not re-solving the package's pixi config")
        return None
    cfg = _pixi_config(manifest) or {}
    ws = cfg.get(_workspace_key(cfg)) or {}
    platforms = [_platform_of(p) for p in ws.get("platforms") or []]
    if plat not in platforms:
        log(f"The package's pixi config does not support {plat}")
        return None
    own = (pixi_config_sysreqs(cfg, plat) if rich
           else cfg.get("system-requirements") or {})
    host = sysreqs_for_platform(sysreqs, plat) if rich else sysreqs_to_table(sysreqs)
    reqs, problems = merge_system_requirements(own, host)
    if problems:
        log("The package needs a newer system: " + "; ".join(problems))
        return None
    # Start from a clean env: the failed locked install may have left one.
    # The package's pixi.lock is kept as pixi.lock.orig (not modified)
    shutil.rmtree(project / ".pixi", ignore_errors=True)
    lock.rename(project / "pixi.lock.orig")
    try:
        if rich:
            name = write_platform_requirements(manifest, plat, reqs)
            if name:
                log(f"System requirements for the re-solve: {reqs} "
                    f"(platform {name!r})")
        elif reqs != own:
            write_system_requirements(manifest, sysreqs_from_table(reqs))
            log(f"System requirements for the re-solve: {reqs}")
        how = None
        if pins:
            kinds = lock_check.kinds if lock_check else {}
            applied = write_pins_to_pixi_config(manifest, pins, kinds, pkg.pkg_name)
            how = "added to the package's pixi config: " + "; ".join(applied)
            log(f"Pinned packages {how}")
    except (RuntimeError, ValueError, KeyError) as e:
        log(f"Could not adapt the package's pixi config: {e}")
        return None
    log(f"Solving {pkg.pkg_name}=={version} from its pixi config")
    no_build: list[str] = []

    def _install(nb: list[str]) -> None:
        if nb:
            write_pypi_no_build(manifest, nb)
        _run_install(
            [pixi, "install", "--manifest-path", str(manifest)], log, src.env
        )

    try:
        _install_with_no_build_retry(_install, pkg.pkg_name, no_build, log)
        # A re-solved env can drift like the resolved one: pins must hold
        return _done(ENV_PIXI_CONFIG, pins, how=how, no_build=no_build)
    except RuntimeError as e:
        log(f"Install from the package's pixi config failed: {e}")
    return None


def _install_from_wheel(
    pkg, version, info, extras, pixi, log, pins, sysreqs: dict,
    rich: bool = True,
) -> tuple[dict, dict]:
    """(c) generated pixi.toml with the wheel (or sdist) as a pypi
    dependency, and the pins as pypi dependencies. Tries each allowed
    Python; a failed source build of a dependency is retried with source
    builds of that package disabled."""
    source = resolve_install_source(pkg, version, info.requires_python)
    log(f"Installing {pkg.pkg_name}=={version} from {source.description}")
    if pins:
        log(f"Pinned packages (pypi-dependencies): {_pins_text(pins)}")
    errors = []
    no_build: list[str] = []
    for i, py in enumerate(source.pythons):
        def _install(nb: list[str], py=py) -> None:
            manifest = write_pixi_manifest(
                pkg, version, extras, info.requires_python, pins=pins,
                python=py, source=source, system_requirements=sysreqs,
                no_build=nb, rich_platforms=rich,
            )
            log(f"Installing {pkg.pkg_name}=={version} (Python {py}) "
                f"into {manifest.parent}")
            _run_streaming(
                [pixi, "install", "--manifest-path", str(manifest)], log
            )

        try:
            _install_with_no_build_retry(_install, pkg.pkg_name, no_build, log)
            break
        except RuntimeError as e:
            errors.append((py, e))
        if i + 1 < len(source.pythons):
            log(f"Install with Python {py} failed; retrying with "
                f"Python {source.pythons[i + 1]}")
    else:
        py0, first = errors[0]
        tried = ", ".join(dict.fromkeys(p for p, _ in errors))
        with_pins = f" with the pinned packages {_pins_text(pins)}" if pins else ""
        raise RuntimeError(
            f"Install failed{with_pins} with every allowed Python ({tried}). "
            f"Error with Python {py0}:\n{first}"
        )
    manifest = env_dir(pkg.pkg_name, version) / "pixi.toml"
    entry, check = _finish_install(
        pkg, version, info, pixi, manifest, ENV_RESOLVED, log, pins=pins,
        pins_how="added to the generated pixi.toml (pypi-dependencies)",
    )
    recipe = _make_recipe(
        pkg, version, ENV_RESOLVED, manifest, python=py, no_build=no_build,
        source=source.description, check=check,
        pins_applied=entry.get("pins_applied"),
    )
    return entry, recipe


def _install_from_recipe(
    pkg, version, info, recipe: dict, pixi: str, log
) -> dict:
    """Restore the manifest + lock of an earlier successful install and
    install them with ``--frozen`` (no solve)."""
    d = env_dir(pkg.pkg_name, version)
    manifest = d / recipe["manifest_relpath"]
    lock = manifest.parent / "pixi.lock"
    kind = recipe["env_kind"]
    env = None
    if kind != ENV_RESOLVED:
        # The package itself is installed from the extracted source
        if not manifest.exists() or manifest.read_text() != recipe["manifest_text"]:
            project = _extract_source(
                _fetch_source(recipe["source_url"], log), d / SOURCE_SUBDIR
            )
            if project.resolve() != manifest.parent.resolve():
                raise RuntimeError("the source archive changed layout")
        if recipe.get("build_env"):
            env = _source_build_env(pkg, version)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    for path, text in ((manifest, recipe["manifest_text"]),
                       (lock, recipe["lock_text"])):
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
    log(f"Installing {pkg.pkg_name}=={version} from the cached install "
        f"recipe ({kind}, --frozen)")
    _run_install(
        [pixi, "install", "--frozen", "--manifest-path", str(manifest)],
        log, env,
    )
    applied = recipe.get("pins_applied") or {}
    entry, _check = _finish_install(
        pkg, version, info, pixi, manifest, kind, log, recipe=recipe,
        pins=applied.get("pins"), pins_how=applied.get("how"),
    )
    entry["from_recipe"] = True
    return entry


def pixi_version(pixi: str) -> str:
    try:
        out = subprocess.run(
            [pixi, "--version"], capture_output=True, text=True, timeout=30
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def install_recipe_key(
    pkg: TaskPackage,
    version: str,
    extras: list[str],
    pins: Optional[list[str]],
    sysreqs: dict,
    pixi: str,
    use_lock: bool = True,
    pixi_ver: Optional[str] = None,
) -> str:
    """Key of the install recipe: everything that decides what gets
    installed. (The release is identified by the catalog's wheel URL, so
    computing the key needs no network.) The pixi version also decides the
    manifest syntax (use_rich_platforms)."""
    pv = pkg.get_version(version)
    return _env_cache.recipe_key({
        "pkg": pkg.pkg_name,
        "version": version,
        "source": pv.wheel_url,
        "platform": current_pixi_platform(),
        "system_requirements": sysreqs,
        "pins": parse_pins(pins),
        "extras": sorted(extras),
        "pixi": pixi_ver if pixi_ver is not None else pixi_version(pixi),
        "pythons": list(PREFERRED_PYTHONS),
        "use_lock": bool(use_lock),
    })


def install_version(
    pkg: TaskPackage,
    version: str,
    extras: Optional[list[str]] = None,
    log: Callable[[str], None] = print,
    pins: Optional[list[str]] = None,
    use_lock: bool = True,
) -> dict:
    """
    Create/update the pixi env for (package, version), check that its task
    modules import, and locate the installed package. Returns a dict
    compatible with ``PackageImporter.loaded_packages`` entries
    (``pixi_frozen``: run with ``pixi run --frozen``; ``env_kind``: one of
    "pixi.lock", "pixi config", "resolved").

    Order: a cached install recipe for exactly these inputs (``--frozen``),
    else, with ``use_lock`` (default) and a release that ships a pixi.lock:
    (a) its pixi.lock as-is (``pixi install --frozen``; extras are not
    used: exact, tested dependency versions) if it covers this platform
    and satisfies every pin (checked before installing), (b) else a
    re-solve of its pixi config with the host's system requirements and
    the pins added to it (skipped with extras); then (c) a generated
    pixi.toml pinning ``pkg==version`` (+ extras and pins), with
    Python fallback, no-build retry and sdist fallback for GitHub releases.
    Pinned versions win: they are never ignored. Every path checks them on
    the installed env (a violation falls through to the next path, or
    fails on (c)); ``entry["pins_applied"]`` says how they were applied.
    Every path ends with the native-library dedup and the import check
    (``check_env``); a failed check falls through to the next path.

    The env is marked incomplete until everything succeeded, so a failed
    install is kept for debugging but not reported by ``is_installed``.
    """
    info = get_version_info(pkg, version)
    extras = list(extras or [])
    pixi = find_pixi()
    d = env_dir(pkg.pkg_name, version)
    d.mkdir(parents=True, exist_ok=True)
    incomplete = d / INCOMPLETE_MARKER
    incomplete.write_text(
        "Install started " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
    )

    sysreqs = (
        host_system_requirements(log) if DECLARE_SYSTEM_REQUIREMENTS else {}
    )
    pixi_ver = pixi_version(pixi)
    rich = use_rich_platforms(pixi_ver)
    if sysreqs:
        log(f"System requirements: {sysreqs} ("
            + ("on the platform entry" if rich else
               f"[system-requirements] table: {pixi_ver} predates "
               "per-platform requirements") + ")")
    key = install_recipe_key(
        pkg, version, extras, pins, sysreqs, pixi, use_lock, pixi_ver=pixi_ver
    )

    recipe = _env_cache.load_recipe(recipes_dir(), key, log)
    if recipe is not None:
        try:
            entry = _install_from_recipe(pkg, version, info, recipe, pixi, log)
            entry["recipe_key"] = key
            incomplete.unlink(missing_ok=True)
            return entry
        except Exception as e:  # noqa: BLE001
            log(f"Install from the cached recipe failed ({type(e).__name__}: "
                f"{e}); deleting the recipe and installing from scratch")
            _env_cache.drop_recipe(recipes_dir(), key)
            # Don't let the full flow start from the restored (maybe bad)
            # lock file
            restored = d / recipe.get("manifest_relpath", "pixi.toml")
            (restored.parent / "pixi.lock").unlink(missing_ok=True)

    entry = new_recipe = None
    if use_lock:
        src = None
        try:
            src = _prepare_source(pkg, version, log)
        except Exception as e:  # noqa: BLE001
            log(f"Could not use the source archive: {type(e).__name__}: {e}")
            shutil.rmtree(d / SOURCE_SUBDIR, ignore_errors=True)
        if src is not None:
            try:
                result = _install_from_source(
                    pkg, version, info, src, pixi, log, extras, pins, sysreqs,
                    rich=rich,
                )
            except RuntimeError as e:
                log(f"{e}")
                result = None
            if result is not None:
                entry, new_recipe = result
            else:
                log("Falling back to resolving dependencies")
                shutil.rmtree(d / SOURCE_SUBDIR, ignore_errors=True)

    if entry is None:
        entry, new_recipe = _install_from_wheel(
            pkg, version, info, extras, pixi, log, pins, sysreqs, rich=rich
        )
    if new_recipe is not None and _env_cache.recipes_enabled():
        try:
            _env_cache.save_recipe(recipes_dir(), key, new_recipe)
            entry["recipe_key"] = key
        except OSError as e:
            log(f"Could not save the install recipe: {e}")
    incomplete.unlink(missing_ok=True)
    return entry


def is_installed(pkg_name: str, version: str) -> bool:
    d = env_dir(pkg_name, version)
    if (d / INCOMPLETE_MARKER).exists():
        return False  # failed or interrupted install (kept for debugging)
    if (d / "pixi.toml").exists() and (d / ".pixi" / "envs").exists():
        return True
    src = d / SOURCE_SUBDIR
    return src.exists() and any(src.glob("*/.pixi/envs"))


# delete_env keeps native libraries orphaned less than this long, so that
# re-installing a just-deleted env still finds them (seconds)
PRUNE_UNUSED_FOR = 14 * 24 * 3600


def prune_native_store(unused_for: float = 0.0) -> dict:
    """Remove native-library store entries no env links to any more
    (and unused for ``unused_for`` seconds)."""
    stats = _env_cache.prune_store(native_store_dir(), unused_for=unused_for)
    return {"removed": stats.removed, "freed_bytes": stats.freed_bytes,
            "kept": stats.kept}


def delete_env(
    pkg_name: str, version: str, prune_unused_for: Optional[float] = None
) -> dict:
    """Delete the env of (package, version), then prune native libraries
    that no env has used for ``prune_unused_for`` seconds (default
    PRUNE_UNUSED_FOR; 0 = all unused ones). Install recipes are kept, so a
    re-install is a ``--frozen`` install. Returns the prune stats."""
    d = env_dir(pkg_name, version)
    if d.exists():
        shutil.rmtree(d)
    return prune_native_store(
        PRUNE_UNUSED_FOR if prune_unused_for is None else prune_unused_for
    )


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
