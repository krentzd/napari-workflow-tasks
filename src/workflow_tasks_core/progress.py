"""
Progress of slow steps (installing a task package, loading the catalog):
which install step a log line of :func:`install.install_version` starts, and
the elapsed time as short text.
"""

from __future__ import annotations

from typing import Optional

# Install log line prefix -> the step shown in the progress bar
INSTALL_STEPS = (
    (("Looking for pixi.lock", "Using the cached", "Could not download"),
     "Downloading the package source"),
    (("Installing", "Solving", "Install with Python"),
     "Installing dependencies (this can take several minutes)"),
    (("Caching the environment", "Native-library"),
     "Caching the environment"),
    (("Checking that the tasks import", "Import check"),
     "Checking that the tasks import"),
)


def install_step(line: str) -> Optional[str]:
    """The progress step an install log line starts, or None."""
    line = line.strip()
    for prefixes, step in INSTALL_STEPS:
        if line.startswith(prefixes):
            return step
    return None


def format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"
