"""
Installing a task package version in its own pixi environment.

``install_version`` creates the environment of one (package, version):
from a cached install recipe, else from the release's own pixi.lock or
pixi config, else from a generated pixi.toml (see pixi_config). It then
shares native libraries with the store and checks that the tasks import
(see env_cache for the caches).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

# Functions that tests replace (find_pixi, get_version_info, http_get, ...)
# and settings (DECLARE_SYSTEM_REQUIREMENTS, PREFERRED_PYTHONS) are used
# through their module, so that the replacement is seen here.
from . import env_cache, manifests, pixi_config, task_packages
from .manifests import VersionInfo
from .paths import cache_dir, env_dir, native_store_dir, recipes_dir
from .pixi_config import (
    check_pins_against_lock,
    lock_platforms,
    merge_system_requirements,
    parse_pins,
    parse_skip,
    pixi_config_sysreqs,
    platform_of,
    read_pixi_config,
    resolve_install_source,
    sysreqs_for_platform,
    sysreqs_from_table,
    sysreqs_to_table,
    unmet_pins,
    use_rich_platforms,
    workspace_key,
    write_pins_to_pixi_config,
    write_pixi_manifest,
    write_platform_requirements,
    write_pypi_no_build,
    write_skip_to_pixi_config,
    write_system_requirements,
)
from .task_packages import (
    TaskPackage,
    has_pixi_lock,
    is_tag_archive,
    normalize_package_name,
)


# Known-good dependency pins, suggested at install (like fractal-server's
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

# Dependencies to leave out on platforms where they cannot be installed,
# per ``sys.platform``; suggested at install, the user can edit or clear
# them. Tasks that import a skipped package fail when they are run.
SUGGESTED_SKIP = {
    # abbott>=0.4 requires cupy-cuda12x (no macOS wheels); only its
    # warpfield tasks import cupy.
    "abbott": {"darwin": ["cupy-cuda12x"]},
}


def suggested_skip(pkg_name: str) -> list[str]:
    """Dependencies of ``pkg_name`` to skip on this computer."""
    return list(SUGGESTED_SKIP.get(pkg_name, {}).get(sys.platform, []))


# --------------------------------------------------------------------------
# Installation in a dedicated pixi environment
# --------------------------------------------------------------------------


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
    from the package's pixi.lock; see runner.pixi_run_prefix).
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


# --------------------------------------------------------------------------
# Install caches (see env_cache): source archives, native libraries,
# install recipes, import-check records
# --------------------------------------------------------------------------

INCOMPLETE_MARKER = ".install_incomplete"
SOURCE_SUBDIR = "source"  # source archive -> env_dir(pkg, ver)/source/<top>/

# Kinds of environments (entry["env_kind"])
ENV_LOCKED = "pixi.lock"  # (a) the package's own pixi.lock, --frozen
ENV_PIXI_CONFIG = "pixi config"  # (b) its pixi config, re-solved here
ENV_RESOLVED = "resolved"  # (c) generated pixi.toml with the wheel


def _fetch_source(url: str, log: Callable[[str], None]) -> bytes:
    """The source archive, from the local cache or downloaded into it
    (releases are immutable)."""
    name = os.path.basename(urlparse(url).path) or "source.tar.gz"
    d = cache_dir() / "sources" / env_cache.text_sha256(url)[:16]
    archive = d / name
    if archive.exists():
        log(f"Using the cached {name}")
        return archive.read_bytes()
    data = task_packages.http_get(url)
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
    if is_tag_archive(url):
        # GitHub's tag archive can't be built by hatch-vcs/setuptools-scm
        # packages (no PKG-INFO): prefer the release's sdist asset
        asset = task_packages.release_sdist_guess(pv)
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
    skip: Optional[list[str]] = None,
) -> tuple[dict, dict]:
    """After a successful ``pixi install``: check pins (when given), share
    native libraries with the store, then run the import check unless it
    already passed for this exact env. Returns (entry, check record).
    ``entry["pins_applied"]``: how the pins were applied (``pins_how``),
    and the installed versions of the pinned packages;
    ``entry["skipped"]``: the dependencies left out of the environment."""
    frozen = kind == ENV_LOCKED
    pins_applied = None
    if pins:
        versions = pixi_config.locked_versions(pixi, manifest)
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
    log("Caching the environment's native libraries")
    stats = env_cache.dedup_tree(envs, native_store_dir(), log)
    log(stats.summary())

    prefix = envs / "default"
    lock = manifest.parent / "pixi.lock"
    expected = {
        "lock_sha256": env_cache.file_sha256(lock) if lock.exists() else None,
        "modules": _check_modules(info),
        "env_fingerprint": env_cache.env_fingerprint(prefix),
    }
    marker = env_cache.load_check(prefix)
    recipe_check = (recipe or {}).get("check")
    pkg_dir = None
    if (
        expected["lock_sha256"]
        and env_cache.check_matches(marker, expected)
        and (recipe is None or env_cache.check_matches(
            recipe_check,
            {k: expected[k] for k in ("lock_sha256", "modules")},
        ))
        and os.path.isdir(marker.get("pkg_dir", ""))
    ):
        pkg_dir = marker["pkg_dir"]
        log("Import check skipped: it passed for this exact environment")
    if pkg_dir is None:
        env_cache.clear_check(prefix)
        t0 = time.monotonic()
        log("Checking that the tasks import")
        pkg_dir = check_env(pixi, manifest, info, log, frozen=frozen)
        log(f"Import check took {time.monotonic() - t0:.1f}s")
        record = {**expected, "ok": True, "pkg_dir": pkg_dir,
                  "seconds": round(time.monotonic() - t0, 1)}
        env_cache.save_check(prefix, record)
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
        # Run with `pixi run --frozen` (see runner.pixi_run_prefix)
        "pixi_frozen": frozen,
        "env_kind": kind,
        "version": version,
        "pins_applied": pins_applied,
        "skipped": list(skip or []),
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
    sysreqs: dict, rich: bool = True, skip: Optional[list[str]] = None,
) -> Optional[tuple[dict, dict]]:
    """(a) the release's own pixi.lock (``pixi install --frozen``) when it
    satisfies the pins, else (b) a re-solve of its pixi config with the
    host's system requirements, the pins and the skipped packages. None =
    use the resolved install (c)."""
    manifest, project = src.manifest, src.project
    lock = project / "pixi.lock"
    plat = pixi_config.current_pixi_platform()
    lock_text = lock.read_text(errors="ignore")

    def _done(kind: str, check_pins, how=None, skipped=None,
              **extra) -> tuple[dict, dict]:
        entry, check = _finish_install(
            pkg, version, info, pixi, manifest, kind, log, pins=check_pins,
            pins_how=how, skip=skipped,
        )
        recipe = _make_recipe(
            pkg, version, kind, manifest, source_url=src.url,
            build_env=bool(src.env), check=check,
            pins_applied=entry.get("pins_applied"),
            skipped=entry["skipped"], **extra,
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
        if skip:
            log("Skipped packages are not used for the pixi.lock install "
                "(only if it falls back to resolving dependencies)")
        lock_sha = env_cache.file_sha256(lock)
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
            if lock.exists() and env_cache.file_sha256(lock) != lock_sha:
                log("Warning: the package's pixi.lock was changed by pixi")

    # (b) re-solve the package's own pixi config (lock unusable here)
    if extras:
        log("Extras requested: not re-solving the package's pixi config")
        return None
    cfg = read_pixi_config(manifest) or {}
    ws = cfg.get(workspace_key(cfg)) or {}
    platforms = [platform_of(p) for p in ws.get("platforms") or []]
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
        if skip:
            write_skip_to_pixi_config(manifest, skip)
            log(f"Skipped packages: {', '.join(skip)}")
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
        return _done(ENV_PIXI_CONFIG, pins, how=how, skipped=skip,
                     no_build=no_build)
    except RuntimeError as e:
        log(f"Install from the package's pixi config failed: {e}")
    return None


def _install_from_wheel(
    pkg, version, info, extras, pixi, log, pins, sysreqs: dict,
    rich: bool = True, skip: Optional[list[str]] = None,
) -> tuple[dict, dict]:
    """(c) generated pixi.toml with the wheel (or sdist) as a pypi
    dependency, the pins as pypi dependencies and the skipped packages as
    dependency overrides. Tries each allowed
    Python; a failed source build of a dependency is retried with source
    builds of that package disabled."""
    source = resolve_install_source(pkg, version, info.requires_python)
    log(f"Installing {pkg.pkg_name}=={version} from {source.description}")
    if pins:
        log(f"Pinned packages (pypi-dependencies): {_pins_text(pins)}")
    if skip:
        log(f"Skipped packages: {', '.join(skip)}")
    errors = []
    no_build: list[str] = []
    for i, py in enumerate(source.pythons):
        def _install(nb: list[str], py=py) -> None:
            manifest = write_pixi_manifest(
                pkg, version, extras, info.requires_python, pins=pins,
                python=py, source=source, system_requirements=sysreqs,
                no_build=nb, rich_platforms=rich, skip=skip,
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
        skip=skip,
    )
    recipe = _make_recipe(
        pkg, version, ENV_RESOLVED, manifest, python=py, no_build=no_build,
        source=source.description, check=check,
        pins_applied=entry.get("pins_applied"), skipped=entry["skipped"],
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
        skip=recipe.get("skipped"),
    )
    entry["from_recipe"] = True
    return entry


def install_recipe_key(
    pkg: TaskPackage,
    version: str,
    extras: list[str],
    pins: Optional[list[str]],
    sysreqs: dict,
    pixi: str,
    use_lock: bool = True,
    pixi_ver: Optional[str] = None,
    skip: Optional[list[str]] = None,
) -> str:
    """Key of the install recipe: everything that decides what gets
    installed. (The release is identified by the catalog's wheel URL, so
    computing the key needs no network.) The pixi version also decides the
    manifest syntax (use_rich_platforms)."""
    pv = pkg.get_version(version)
    return env_cache.recipe_key({
        "pkg": pkg.pkg_name,
        "version": version,
        "source": pv.wheel_url,
        "platform": pixi_config.current_pixi_platform(),
        "system_requirements": sysreqs,
        "pins": parse_pins(pins),
        "extras": sorted(extras),
        "pixi": (pixi_ver if pixi_ver is not None
                 else pixi_config.pixi_version(pixi)),
        "pythons": list(task_packages.PREFERRED_PYTHONS),
        "use_lock": bool(use_lock),
        "skip": sorted(parse_skip(skip)),
    })


def install_version(
    pkg: TaskPackage,
    version: str,
    extras: Optional[list[str]] = None,
    log: Callable[[str], None] = print,
    pins: Optional[list[str]] = None,
    use_lock: bool = True,
    skip: Optional[list[str]] = None,
) -> dict:
    """
    Create/update the pixi env for (package, version), check that its task
    modules import, and locate the installed package. Returns the
    installed package's entry (``pixi_frozen``: run with
    ``pixi run --frozen``; ``env_kind``: one of "pixi.lock",
    "pixi config", "resolved").

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

    ``skip``: dependencies to leave out of the env (e.g. packages that
    cannot be installed on this platform, see SUGGESTED_SKIP); used by
    (b) and (c), not by the pixi.lock install (a).

    The env is marked incomplete until everything succeeded, so a failed
    install is kept for debugging but not reported by ``is_installed``.
    """
    info = manifests.get_version_info(pkg, version)
    extras = list(extras or [])
    skip = parse_skip(skip)
    pixi = pixi_config.find_pixi()
    d = env_dir(pkg.pkg_name, version)
    d.mkdir(parents=True, exist_ok=True)
    incomplete = d / INCOMPLETE_MARKER
    incomplete.write_text(
        "Install started " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
    )

    sysreqs = (
        pixi_config.host_system_requirements(log)
        if pixi_config.DECLARE_SYSTEM_REQUIREMENTS else {}
    )
    pixi_ver = pixi_config.pixi_version(pixi)
    rich = use_rich_platforms(pixi_ver)
    if sysreqs:
        log(f"System requirements: {sysreqs} ("
            + ("on the platform entry" if rich else
               f"[system-requirements] table: {pixi_ver} predates "
               "per-platform requirements") + ")")
    key = install_recipe_key(
        pkg, version, extras, pins, sysreqs, pixi, use_lock, pixi_ver=pixi_ver,
        skip=skip,
    )

    recipe = env_cache.load_recipe(recipes_dir(), key, log)
    if recipe is not None:
        try:
            entry = _install_from_recipe(pkg, version, info, recipe, pixi, log)
            entry["recipe_key"] = key
            _save_entry(d, entry, log)
            incomplete.unlink(missing_ok=True)
            return entry
        except Exception as e:  # noqa: BLE001
            log(f"Install from the cached recipe failed ({type(e).__name__}: "
                f"{e}); deleting the recipe and installing from scratch")
            env_cache.drop_recipe(recipes_dir(), key)
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
                    rich=rich, skip=skip,
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
            pkg, version, info, extras, pixi, log, pins, sysreqs, rich=rich,
            skip=skip,
        )
    if new_recipe is not None and env_cache.recipes_enabled():
        try:
            env_cache.save_recipe(recipes_dir(), key, new_recipe)
            entry["recipe_key"] = key
        except OSError as e:
            log(f"Could not save the install recipe: {e}")
    _save_entry(d, entry, log)
    incomplete.unlink(missing_ok=True)
    return entry


# The entry install_version returned, kept in the env folder so that an
# installed package can be run later without the catalog
ENTRY_FILE = "install_entry.json"


def _save_entry(d: Path, entry: dict, log: Callable[[str], None]) -> None:
    try:
        (d / ENTRY_FILE).write_text(json.dumps(entry, indent=1))
    except (OSError, TypeError) as e:
        log(f"Could not save the install entry: {e}")


def installed_entry(pkg_name: str, version: str) -> dict:
    """The install entry of an installed (package, version), read from its
    env folder (no network). Envs installed before entries were saved are
    recognised from their import check (run without ``--frozen``)."""
    if not is_installed(pkg_name, version):
        raise FileNotFoundError(f"{pkg_name} {version} is not installed")
    d = env_dir(pkg_name, version)
    try:
        return json.loads((d / ENTRY_FILE).read_text())
    except (OSError, ValueError):
        pass
    candidates = [d / "pixi.toml"]
    for project in sorted((d / SOURCE_SUBDIR).glob("*")):
        candidates += [project / "pixi.toml", project / "pyproject.toml"]
    for manifest in candidates:
        prefix = manifest.parent / ".pixi" / "envs" / "default"
        record = env_cache.load_check(prefix) if manifest.exists() else None
        if record and record.get("ok") and record.get("pkg_dir"):
            pkg_dir = record["pkg_dir"]
            return {
                "src_path": pkg_dir,
                "manifest_path": os.path.join(pkg_dir, "__FRACTAL_MANIFEST__.json"),
                "pixi_manifest_path": str(manifest),
                "pixi_frozen": False,
                "version": version,
            }
    raise FileNotFoundError(
        f"{pkg_name} {version}: cannot find the installed package; "
        "install it again")


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
    stats = env_cache.prune_store(native_store_dir(), unused_for=unused_for)
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
