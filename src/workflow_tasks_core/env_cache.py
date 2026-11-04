"""
Local caches that make (re-)installing task environments fast.

No Qt and no third-party dependencies; used by ``install.install_version``.

Three layers:

1. Native-library store (``<cache>/native/<sha256>``). pixi creates every
   file of a new environment as a new inode (APFS clone or copy). On macOS
   every new native library is scanned by XProtect / anti-virus on first
   load, which makes the first import of a heavy task package take minutes.
   A hard link to an already-loaded file is not scanned again. So after an
   install every regular ``.so``/``.dylib`` file of the env is either
   replaced by a hard link to an identical file in the store, or added to
   the store. Identical libraries are then shared between environments and
   across re-installs. Disable with ``NAPARI_WORKFLOW_TASKS_DEDUP=0``.

   Safety: hard-linked files must never be written in place. pixi (conda
   and PyPI packages) removes files and creates new ones on updates, so
   other environments are not affected (see the tests and the report).
   Store entries no environment uses any more (link count 1) are removed
   by :func:`prune_store`.

2. Install recipes (``<cache>/recipes/<key>.json``): what a successful
   install did (install path, Python, no-build list, the manifest and the
   resulting lock file). A re-install with the same inputs restores them
   and runs ``pixi install --frozen`` (no solve, no failed attempts).

3. Import-check results: a marker inside the env prefix records that the
   import check passed for a given lock file / module list / set of
   installed packages, so a re-install of an unchanged env skips it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

RECIPE_SCHEMA = 2  # 2: rich-platform manifests, pins_applied

DEDUP_ENV_VAR = "NAPARI_WORKFLOW_TASKS_DEDUP"
RECIPES_ENV_VAR = "NAPARI_WORKFLOW_TASKS_RECIPES"

# libfoo.so, libfoo.so.1.2.3, libfoo.dylib, foo.cpython-312-darwin.so
NATIVE_LIB_RE = re.compile(r"\.(?:so|dylib)(?:\.[0-9]+)*$")

CHECK_MARKER = ".napari_workflow_tasks_check.json"

# os.link errors meaning "hard links are not possible here" (store on
# another filesystem, filesystem without hard links, no permission).
_NO_LINK_ERRNOS = {
    errno.EXDEV, errno.EPERM, errno.EACCES, errno.EROFS,
    getattr(errno, "ENOTSUP", -1), getattr(errno, "EOPNOTSUPP", -1),
}

_TMP_SUFFIX = ".nwt-tmp"


def _flag(var: str, default: bool = True) -> bool:
    value = os.environ.get(var)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def dedup_enabled() -> bool:
    return _flag(DEDUP_ENV_VAR)


def recipes_enabled() -> bool:
    return _flag(RECIPES_ENV_VAR)


def file_sha256(path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------
# 1. Native-library store
# --------------------------------------------------------------------------


@dataclass
class DedupStats:
    scanned: int = 0  # regular native-library files found
    relinked: int = 0  # replaced by a link to an existing store entry
    added: int = 0  # new store entries
    already_linked: int = 0  # link count > 1: done by an earlier pass
    skipped: int = 0  # empty, mode differs from the store entry, errors
    disabled: Optional[str] = None  # why dedup did not run / stopped
    seconds: float = 0.0

    def summary(self) -> str:
        if self.disabled and not self.scanned:
            return f"Native-library dedup off: {self.disabled}"
        text = (
            f"Native libraries: {self.scanned} found, {self.relinked} "
            f"linked to the cache, {self.added} new in the cache, "
            f"{self.already_linked} already linked, {self.skipped} skipped "
            f"({self.seconds:.1f}s)"
        )
        if self.disabled:
            text += f"; stopped: {self.disabled}"
        return text


def iter_native_libs(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Regular (non-symlink) native-library files below ``root``."""
    for dirpath, _dirnames, filenames in os.walk(root):  # no symlinked dirs
        for name in filenames:
            if not NATIVE_LIB_RE.search(name):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield path, st


def _replace_with_link(entry: Path, path: str) -> None:
    """Atomically replace ``path`` by a hard link to ``entry``."""
    tmp = f"{path}.{os.getpid()}{_TMP_SUFFIX}"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.link(entry, tmp)
    try:
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dedup_tree(
    root: Path,
    store: Path,
    log: Callable[[str], None] = print,
) -> DedupStats:
    """
    Share identical native libraries of ``root`` (an env, or a dir of envs)
    with the store: an existing store entry replaces the env file (hard
    link, atomic rename), a new file is hard-linked into the store.

    Never raises for I/O problems: when hard links are impossible (other
    filesystem, EPERM...) it logs once and leaves the env as installed.
    """
    stats = DedupStats()
    t0 = time.monotonic()
    if not dedup_enabled():
        stats.disabled = f"{DEDUP_ENV_VAR}=0"
        return stats
    root = Path(root)
    if not root.is_dir():
        stats.disabled = f"{root} does not exist"
        return stats
    store_ready = False
    for path, st in iter_native_libs(root):
        stats.scanned += 1
        if st.st_nlink > 1:
            # Already shared with the store by an earlier pass (pixi itself
            # always creates files with a single link)
            stats.already_linked += 1
            continue
        if st.st_size == 0:
            stats.skipped += 1
            continue
        try:
            if not store_ready:
                store.mkdir(parents=True, exist_ok=True)
                store_ready = True
            digest = file_sha256(path)
            entry = store / digest
            try:
                os.link(path, entry)
                stats.added += 1
                continue
            except FileExistsError:
                pass
            est = entry.stat()
            if (
                est.st_size != st.st_size
                or stat.S_IMODE(est.st_mode) != stat.S_IMODE(st.st_mode)
            ):
                stats.skipped += 1
                continue
            _replace_with_link(entry, path)
            stats.relinked += 1
        except OSError as e:
            if e.errno in _NO_LINK_ERRNOS:
                stats.disabled = (
                    f"cannot hard-link between {root} and {store} "
                    f"({errno.errorcode.get(e.errno, e.errno)}: {e.strerror})"
                )
                log(f"Native-library dedup skipped: {stats.disabled}")
                break
            stats.skipped += 1
    stats.seconds = time.monotonic() - t0
    return stats


@dataclass
class PruneStats:
    removed: int = 0
    freed_bytes: int = 0
    kept: int = 0


def prune_store(store: Path, unused_for: float = 0.0) -> PruneStats:
    """Remove store entries that no environment links to any more
    (link count 1) and leftover temporary files.

    ``unused_for`` (seconds): only remove entries unused for at least that
    long. Removing an env's link updates the entry's ctime, so ctime is
    "unused since"; keeping recently orphaned entries keeps a re-install
    of a just-deleted env fast."""
    stats = PruneStats()
    store = Path(store)
    if not store.is_dir():
        return stats
    now = time.time()
    for entry in store.iterdir():
        try:
            st = entry.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue
            unused = st.st_nlink <= 1 and now - st.st_ctime >= unused_for
            if unused or entry.name.endswith(_TMP_SUFFIX):
                entry.unlink()
                stats.removed += 1
                stats.freed_bytes += st.st_size
            else:
                stats.kept += 1
        except OSError:
            continue
    return stats


def store_stats(store: Path) -> dict:
    n = size = 0
    if Path(store).is_dir():
        for entry in Path(store).iterdir():
            try:
                size += entry.lstat().st_size
                n += 1
            except OSError:
                pass
    return {"entries": n, "bytes": size}


# --------------------------------------------------------------------------
# 2. Install recipes
# --------------------------------------------------------------------------


def recipe_key(fields: dict) -> str:
    """Stable key for the inputs of an install (dict order irrelevant)."""
    data = json.dumps(
        {"recipe_schema": RECIPE_SCHEMA, **fields},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(data.encode()).hexdigest()[:40]


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}{_TMP_SUFFIX}")
    tmp.write_text(text)
    os.replace(tmp, path)


def recipe_path(recipes_dir: Path, key: str) -> Path:
    return Path(recipes_dir) / f"{key}.json"


def load_recipe(
    recipes_dir: Path, key: str, log: Callable[[str], None] = print
) -> Optional[dict]:
    """The recipe for ``key``, or None. A damaged recipe is deleted."""
    if not recipes_enabled():
        return None
    path = recipe_path(recipes_dir, key)
    if not path.exists():
        return None
    try:
        recipe = json.loads(path.read_text())
        ok = (
            recipe.get("recipe_schema") == RECIPE_SCHEMA
            and recipe.get("key") == key
            and isinstance(recipe.get("lock_text"), str)
            and text_sha256(recipe["lock_text"]) == recipe.get("lock_sha256")
            and isinstance(recipe.get("manifest_text"), str)
        )
    except (OSError, ValueError, AttributeError):
        ok = False
    if not ok:
        log(f"Install recipe {path.name} is damaged: deleting it")
        drop_recipe(recipes_dir, key)
        return None
    return recipe


def save_recipe(recipes_dir: Path, key: str, recipe: dict) -> Path:
    recipe = {
        **recipe,
        "recipe_schema": RECIPE_SCHEMA,
        "key": key,
        "lock_sha256": text_sha256(recipe["lock_text"]),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = recipe_path(recipes_dir, key)
    _atomic_write_text(path, json.dumps(recipe, indent=1))
    return path


def drop_recipe(recipes_dir: Path, key: str) -> None:
    try:
        recipe_path(recipes_dir, key).unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# 3. Import-check results
# --------------------------------------------------------------------------


def env_fingerprint(prefix: Path) -> Optional[str]:
    """Hash of the installed package records of an env prefix (conda-meta
    records + Python dist-info dirs). Changes whenever a package is added,
    removed or changes version; cheap (directory listings only)."""
    prefix = Path(prefix)
    meta = prefix / "conda-meta"
    if not meta.is_dir():
        return None
    names = sorted(p.name for p in meta.glob("*.json"))
    for sp in list(prefix.glob("lib/python*/site-packages")) + [
        prefix / "Lib" / "site-packages"
    ]:
        if sp.is_dir():
            names += sorted(
                f"{sp.relative_to(prefix)}/{p.name}"
                for p in sp.iterdir()
                if p.name.endswith((".dist-info", ".egg-info", ".pth"))
            )
    return hashlib.sha256("\n".join(names).encode()).hexdigest()


def load_check(prefix: Path) -> Optional[dict]:
    try:
        return json.loads((Path(prefix) / CHECK_MARKER).read_text())
    except (OSError, ValueError):
        return None


def save_check(prefix: Path, record: dict) -> None:
    prefix = Path(prefix)
    if prefix.is_dir():
        _atomic_write_text(prefix / CHECK_MARKER, json.dumps(record, indent=1))


def clear_check(prefix: Path) -> None:
    try:
        (Path(prefix) / CHECK_MARKER).unlink()
    except FileNotFoundError:
        pass


def check_matches(record: Optional[dict], expected: dict) -> bool:
    return bool(record) and record.get("ok") is True and all(
        record.get(k) == v for k, v in expected.items()
    )


__all__ = [
    "DedupStats", "PruneStats", "check_matches", "clear_check",
    "dedup_enabled", "dedup_tree", "drop_recipe", "env_fingerprint",
    "file_sha256", "iter_native_libs", "load_check", "load_recipe",
    "prune_store", "recipe_key", "recipes_enabled", "save_check",
    "save_recipe", "store_stats", "text_sha256",
]
