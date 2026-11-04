"""
pixi configuration of task package environments.

Installation pins the exact version (``pkg==X`` / the wheel URL) inside a
dedicated pixi environment, one per (package, version). This module finds
pixi, detects the host's platform and system requirements, writes the
generated pixi.toml, edits a package's own pixi config (system
requirements, pins, no-build) and reads its pixi.lock.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

# PREFERRED_PYTHONS and release_sdist_guess are used through their module
# (settings and tests replace them)
from . import task_packages
from .paths import env_dir
from .task_packages import (
    TaskPackage,
    github_release_assets,
    is_sdist,
    is_tag_archive,
    normalize_package_name,
    wheel_is_compatible,
    wheel_name_version,
)


# --------------------------------------------------------------------------
# pixi, the host platform and system requirements
# --------------------------------------------------------------------------


def find_pixi() -> str:
    exe = shutil.which("pixi")
    if exe:
        return exe
    # an app launched from a GUI often doesn't inherit the shell PATH
    for candidate in (
        Path.home() / ".pixi" / "bin" / "pixi",
        Path.home() / ".pixi" / "bin" / "pixi.exe",
    ):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "Could not find the `pixi` executable. Install it from https://pixi.sh"
    )


def pixi_version(pixi: str) -> str:
    try:
        out = subprocess.run(
            [pixi, "--version"], capture_output=True, text=True, timeout=30
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


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
        return list(task_packages.PREFERRED_PYTHONS)
    try:
        spec = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return list(task_packages.PREFERRED_PYTHONS)
    allowed = [
        py for py in task_packages.PREFERRED_PYTHONS
        if Version(py + ".0") in spec or Version(py + ".99") in spec
    ]
    return allowed or list(task_packages.PREFERRED_PYTHONS)


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


# --------------------------------------------------------------------------
# Generated pixi.toml (install path (c))
# --------------------------------------------------------------------------


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
    assets = github_release_assets(pv)
    for name, url in assets.items():
        if name.endswith(".whl") and name != pv.wheel_filename:
            try:
                if wheel_name_version(name) != (pkg.pkg_name, version):
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
    if pv.source_url and not is_tag_archive(pv.source_url):
        sdist_url = pv.source_url
    else:
        sdists = [n for n in assets if is_sdist(n)]
        if sdists:
            name = next((n for n in sdists if n.endswith(".tar.gz")), sdists[0])
            sdist_url = assets[name]
        else:
            guess = task_packages.release_sdist_guess(pv)
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


# --------------------------------------------------------------------------
# The package's own pixi config and pixi.lock (paths (a) and (b))
# --------------------------------------------------------------------------


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
    return tomllib.loads(text)


def _pixi_section(doc: dict, manifest: Path) -> Optional[dict]:
    if manifest.name == "pyproject.toml":
        return doc.get("tool", {}).get("pixi")
    return doc


def read_pixi_config(manifest: Path) -> Optional[dict]:
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


def workspace_key(cfg: dict) -> str:
    for key in ("workspace", "project"):
        if "platforms" in (cfg.get(key) or {}):
            return key
    return "workspace"


def platform_of(entry) -> Optional[str]:
    return entry.get("platform") if isinstance(entry, dict) else entry


def pixi_config_sysreqs(cfg: dict, platform_: str) -> dict:
    """System requirements the package's pixi config declares for
    ``platform_``, in either syntax: the (deprecated)
    ``[system-requirements]`` table and/or its ``platforms`` entry."""
    own = sysreqs_for_platform(
        sysreqs_from_table(cfg.get("system-requirements") or {}), platform_)
    for e in (cfg.get(workspace_key(cfg)) or {}).get("platforms") or []:
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
    wkey = workspace_key(cfg)
    table = sysreqs_from_table(cfg.get("system-requirements") or {})
    rename: dict = {}
    entries, host_name, done = [], None, False
    for e in cfg[wkey]["platforms"]:
        p = platform_of(e)
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
    cfg = read_pixi_config(manifest) or {}
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
    cfg = read_pixi_config(manifest) or {}
    opts = dict(cfg.get("pypi-options") or {})
    current = opts.get("no-build")
    if current is True:
        raise RuntimeError("source builds are already disabled for all packages")
    merged = list(dict.fromkeys([*(current or []), *names]))
    opts["no-build"] = merged
    _replace_pixi_table(manifest, "pypi-options", opts)
    return merged


def locked_versions(pixi: str, manifest: Path) -> dict[str, str]:
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
    cfg = read_pixi_config(manifest) or {}
    declared = {}
    for e in (cfg.get(workspace_key(cfg)) or {}).get("platforms") or []:
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
