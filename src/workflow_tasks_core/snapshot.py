"""
Pre-built catalog snapshot, and listing the catalog.

Like the Fractal task page, a pre-built snapshot of the catalog (every
version of every package, with task summaries) is published daily by a
GitHub Action (``scripts/build_catalog.py``). The plugin loads that snapshot
with a single request and only falls back to querying PyPI/GitHub live when
it is unavailable, so it does not spend the GitHub API rate limit.
"""

from __future__ import annotations

import json
import os
from typing import Iterator, Optional

# CATALOG_CACHE_DIR and the functions that tests replace are used through
# their module
from . import manifests, paths, task_packages
from .manifests import get_display_info
from .task_packages import PackageVersion, TaskPackage, fetch_sources


# Snapshot published by .github/workflows/build_catalog.yaml (branch `catalog`)
SNAPSHOT_URL = os.environ.get(
    "NAPARI_FRACTAL_TASKS_CATALOG_URL",
    "https://raw.githubusercontent.com/rhornb/napari-fractal-tasks/catalog/",
)
SNAPSHOT_SCHEMA = 1


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
    cache = paths.CATALOG_CACHE_DIR / "snapshot_index.json"
    try:
        raw = task_packages.http_get(f"{base_url}index.json")
        index = json.loads(raw)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
    except Exception:  # noqa: BLE001
        if not cache.exists():
            raise
        index = json.loads(cache.read_text())
    return index.get("generated_at", "?"), packages_from_index(index, base_url)


# --------------------------------------------------------------------------
# Listing the catalog (snapshot, else live)
# --------------------------------------------------------------------------


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
        pkg = task_packages.load_package(source)
        info = None
        if pkg.default_version is not None:
            info = manifests.get_version_info(pkg, pkg.default_version.version)
        return pkg, info

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_load, s): s for s in sources}
        for fut in as_completed(futures):
            source = futures[fut]
            try:
                yield "package", fut.result()
            except Exception as e:  # noqa: BLE001
                yield "error", (source, f"{type(e).__name__}: {e}")
