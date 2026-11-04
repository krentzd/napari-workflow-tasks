"""
Read what an OME-Zarr image holds from its metadata alone: its label images
and its channel labels (NGFF v0.4 ``.zattrs`` or v0.5 ``zarr.json``).

Used to find the labels a task added to an image and to suggest channel
names in task forms.
"""

from __future__ import annotations

import json
from pathlib import Path


def list_labels(zarr_url: str) -> list[str]:
    """Label names of an OME-Zarr image (NGFF v0.4 or v0.5), [] if none."""
    labels_dir = Path(zarr_url) / 'labels'
    for fname, keys in (('.zattrs', ('labels',)), ('zarr.json', ('attributes', 'ome', 'labels'))):
        try:
            with open(labels_dir / fname) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for key in keys:
            data = data.get(key, {}) if isinstance(data, dict) else {}
        if isinstance(data, list):
            return data
    return []


def channel_labels(zarr_url: str) -> list[str]:
    """Channel labels from the OME-Zarr "omero" metadata (v0.4 / v0.5)."""
    root = Path(zarr_url)
    for fname, keys in (('.zattrs', ('omero',)), ('zarr.json', ('attributes', 'ome', 'omero'))):
        try:
            with open(root / fname) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for key in keys:
            data = data.get(key, {}) if isinstance(data, dict) else {}
        channels = data.get('channels', []) if isinstance(data, dict) else []
        labels = [c.get('label') for c in channels if isinstance(c, dict) and c.get('label')]
        if labels:
            return labels
    return []
