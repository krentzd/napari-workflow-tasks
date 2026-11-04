"""
Fractal task packages and their available versions.

Mirrors how the Fractal ecosystem discovers tasks:

* The list of known task packages is the same ``sources.txt`` used to build
  the public Fractal task list
  (fractal-analytics-platform.github.io/tasks_data_retrieval/sources.txt).
  Entries are either PyPI projects or GitHub repositories.
* Versions come from the PyPI "simple" JSON API (like fractal-server's
  ``get_package_version_from_pypi``) or from GitHub releases with wheel
  assets. They are sorted newest-first with ``packaging.version.Version``
  (like fractal-server's ``_version_sort_key``).

Also holds the HTTP / GitHub API helpers used by the other catalog modules
and the choice of a wheel for this machine.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from packaging import tags as packaging_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from . import paths  # paths.CATALOG_CACHE_DIR: tests replace it


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


def http_get(url: str, headers: Optional[dict] = None) -> bytes:
    req_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    req_headers.update(headers or {})
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        data = res.read()
        if res.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def http_get_json(url: str, headers: Optional[dict] = None):
    return json.loads(http_get(url, headers))


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

    cache = paths.CATALOG_CACHE_DIR / "_github" / (
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


def wheel_name_version(filename: str) -> tuple[str, str]:
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


def is_sdist(filename: str) -> bool:
    return filename.endswith((".tar.gz", ".zip"))


# --------------------------------------------------------------------------
# Sources and versions
# --------------------------------------------------------------------------


def fetch_sources(url: str = SOURCES_URL) -> list[str]:
    """Read the official list of task-package sources (fallback: snapshot)."""
    try:
        text = http_get(url).decode()
    except Exception:  # noqa: BLE001
        return list(DEFAULT_SOURCES)
    sources = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return sources or list(DEFAULT_SOURCES)


def _list_pypi_versions(project: str) -> list[PackageVersion]:
    data = http_get_json(
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
        _, version = wheel_name_version(name)
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
        name, version = wheel_name_version(chosen)
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
            pkg_name, _ = wheel_name_version(versions[0].wheel_filename)
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


def github_release_assets(pv: PackageVersion) -> dict[str, str]:
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


def is_tag_archive(url: Optional[str]) -> bool:
    return bool(url) and "/archive/refs/tags/" in url


def release_sdist_guess(pv: PackageVersion) -> Optional[tuple[str, Optional[int]]]:
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
