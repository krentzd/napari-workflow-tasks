"""
Where napari-workflow-tasks keeps its files.

Everything lives under ``ROOT_DIR`` (``~/.napari_workflow_tasks``, or
``$NAPARI_WORKFLOW_TASKS_ROOT``): the catalog cache, one pixi environment
per (package, version), the run files of each task and the install caches.

Other modules read these globals through this module (e.g.
``paths.CATALOG_CACHE_DIR``) so that tests can point them elsewhere.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


ROOT_DIR = Path(
    os.environ.get("NAPARI_WORKFLOW_TASKS_ROOT")
    or Path.home() / ".napari_workflow_tasks"
)
CATALOG_CACHE_DIR = ROOT_DIR / "catalog"
ENVS_DIR = ROOT_DIR / "envs"
# Run files of each task (arguments JSON, --out-json):
# TASKS_DIR / <package> / <version> / <task name>
TASKS_DIR = ROOT_DIR / "tasks"
# Install caches (see env_cache): native-library store, install recipes,
# source archives. None = ENVS_DIR.parent / "cache" (see cache_dir())
CACHE_DIR: Optional[Path] = None


def env_dir(pkg_name: str, version: str) -> Path:
    return ENVS_DIR / pkg_name / version


def _safe_dir_name(name: str) -> str:
    """Folder name for ``name``: path separators and characters Windows
    does not allow become "_"."""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .") or "_"


def task_run_dir(pkg_name: str, version: str, task_name: str) -> Path:
    """Folder for the run files of one task of an installed package."""
    return (TASKS_DIR / _safe_dir_name(pkg_name) / _safe_dir_name(version)
            / _safe_dir_name(task_name))


def cache_dir() -> Path:
    """Root of the install caches (default: next to ENVS_DIR)."""
    return Path(CACHE_DIR) if CACHE_DIR is not None else ENVS_DIR.parent / "cache"


def native_store_dir() -> Path:
    return cache_dir() / "native"


def recipes_dir() -> Path:
    return cache_dir() / "recipes"
