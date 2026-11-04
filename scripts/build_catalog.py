"""
Build the Fractal task catalog snapshot used by napari-workflow-tasks.

Same idea as the Fractal task page (fractal-analytics-platform.github.io,
``tasks_data_retrieval/create_tasks_data.py``), but for *every* version of
every package instead of only the latest:

* sources: the official ``sources.txt`` (PyPI projects and GitHub repos)
* versions: PyPI simple JSON API / GitHub releases with a wheel asset
* tasks: ``__FRACTAL_MANIFEST__.json`` read from each wheel (via HTTP range
  requests, so only a few KB per wheel are downloaded)

Output (in ``--out``)::

    index.json                       # all packages/versions + task summaries
    manifests/<pkg>/<version>.json   # full manifest + metadata per version

Manifests of released versions never change, so existing files in ``--out``
are reused and only new versions are fetched (incremental builds).

Usage::

    GITHUB_TOKEN=... python scripts/build_catalog.py --out catalog-out
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

# Run from a checkout without installing the package (the CI job only
# installs `packaging`): make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_tasks_core import (  # noqa: E402
    manifests,
    snapshot,
    task_packages,
)


def _version_entry(pkg, pv, out: Path, errors: list) -> dict | None:
    manifest_file = out / "manifests" / pkg.pkg_name / f"{pv.version}.json"
    if manifest_file.exists():
        info = manifests.VersionInfo(**json.loads(manifest_file.read_text()))
    else:
        try:
            info = manifests.read_remote_wheel_info(pv.wheel_url, pv.wheel_filename)
        except Exception as e:  # noqa: BLE001
            # e.g. very old releases without a Fractal manifest
            errors.append(
                {"pkg_name": pkg.pkg_name, "version": pv.version, "error": str(e)}
            )
            return None
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(json.dumps(asdict(info)))
    lock_cache = out / "manifests" / pkg.pkg_name / f"{pv.version}.pixi_lock"
    if lock_cache.exists():
        pv.pixi_lock = lock_cache.read_text().strip() == "1"
    elif task_packages.has_pixi_lock(pv) is not None:
        lock_cache.write_text("1" if pv.pixi_lock else "0")
    entry = asdict(pv)
    entry["summary"] = manifests.summarize_info(info)
    return entry


def build(out: Path, sources: list[str], workers: int = 8) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    packages, errors = [], []

    # Previous build: used when a source is temporarily unreachable
    previous, old = {}, {}
    if (out / "index.json").exists():
        try:
            old = json.loads((out / "index.json").read_text())
            previous = {p["source"]: p for p in old.get("packages", [])}
        except ValueError:
            previous, old = {}, {}

    for source in sources:
        t0 = time.perf_counter()
        try:
            pkg = task_packages.load_package(source)
        except Exception as e:  # noqa: BLE001
            print(f"!! {source}: {e}", flush=True)
            errors.append({"source": source, "error": str(e)})
            if source in previous:
                print("   keeping entry from the previous build", flush=True)
                packages.append(previous[source])
            continue

        with ThreadPoolExecutor(max_workers=workers) as pool:
            entries = list(
                pool.map(
                    lambda pv, pkg=pkg: _version_entry(pkg, pv, out, errors),
                    pkg.versions,
                )
            )
        versions = [e for e in entries if e is not None]
        print(
            f"{pkg.pkg_name}: {len(versions)}/{len(pkg.versions)} versions "
            f"with a manifest ({time.perf_counter() - t0:.1f}s)",
            flush=True,
        )
        if versions:
            packages.append(
                {
                    "source": source,
                    "kind": pkg.kind,
                    "pkg_name": pkg.pkg_name,
                    "versions": versions,  # newest first
                }
            )

    packages.sort(key=lambda p: p["pkg_name"].lower())
    errors.sort(key=lambda e: (e.get("source", ""), e.get("pkg_name", ""), e.get("version", "")))
    index = {
        "schema": snapshot.SNAPSHOT_SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": sources,
        "packages": packages,
        "errors": errors,
    }
    # Keep the timestamp if nothing changed, so the file is byte-identical
    # and the CI job has nothing to commit ("generated_at" = last change)
    if old and all(old.get(k) == index[k] for k in ("schema", "sources", "packages", "errors")):
        index["generated_at"] = old.get("generated_at", index["generated_at"])
    (out / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--sources-url",
        default=task_packages.SOURCES_URL,
        help="URL of the list of task-package sources",
    )
    parser.add_argument(
        "--only", nargs="*", default=None,
        help="Only process sources containing one of these strings",
    )
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    sources = task_packages.fetch_sources(args.sources_url)
    if args.only:
        sources = [s for s in sources if any(o in s for o in args.only)]

    index = build(args.out, sources, workers=args.workers)
    n_versions = sum(len(p["versions"]) for p in index["packages"])
    print(
        f"\nWrote {args.out / 'index.json'}: {len(index['packages'])} packages, "
        f"{n_versions} versions, {len(index['errors'])} skipped"
    )
    if not index["packages"]:
        sys.exit("No package could be loaded")


if __name__ == "__main__":
    main()
