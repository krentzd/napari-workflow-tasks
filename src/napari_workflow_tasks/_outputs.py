"""Work out what a task wrote from the arguments it was run with.

Fractal tasks name their outputs from their arguments, so the plugin can
show exactly those outputs after a run instead of guessing:

- label images: ``label_name`` / ``output_label_name``, where
  ``{channel_identifier}`` is the channel the task segmented
  (``channel.identifier``, or the first of ``channels.identifiers``);
- new images: what the task reports in its ``--out-json`` file
  (``image_list_updates``), plus ``output_image_suffix`` (written next to the
  input as ``<image>_<suffix>``) and ``output_image_name`` templates that only
  use ``{image_name}``.

``args`` are the arguments sent to the task. Arguments left at their default
are not sent, so the schema default (in ``props``) is used for them.
"""

from __future__ import annotations

import json
import os
import string
from typing import Any, Optional

LABEL_NAME_ARGS = ('label_name', 'output_label_name')
CHANNEL_PLACEHOLDER = '{channel_identifier}'


def _arg(args: dict, props: dict, key: str) -> Any:
    if key in args:
        return args[key]
    return props.get(key, {}).get('default')


def channel_identifier(args: dict) -> Optional[str]:
    """The channel a task works on: ``identifier`` / first of ``identifiers``
    in an argument model whose name contains "channel"."""
    for key, value in args.items():
        if not isinstance(value, dict):
            continue
        if 'channel' in key.lower():
            ident = value.get('identifier')
            if isinstance(ident, str) and ident:
                return ident
            idents = value.get('identifiers')
            if isinstance(idents, list) and idents:
                return str(idents[0])
        found = channel_identifier(value)
        if found:
            return found
    return None


def _fill(template: str, **values: str) -> Optional[str]:
    """Fill ``{name}`` placeholders; None if one has no value."""
    fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
    if not fields <= set(values):
        return None
    return template.format(**values)


def expected_label_names(args: dict, props: dict) -> list[str]:
    """Label images the task writes into its input image."""
    names = []
    channel = channel_identifier(args)
    for key in LABEL_NAME_ARGS:
        if key not in props and key not in args:
            continue
        template = _arg(args, props, key)
        if not isinstance(template, str) or not template:
            continue
        values = {'channel_identifier': channel} if channel else {}
        name = _fill(template, **values)
        if name:
            names.append(name)
    return names


def expected_image_urls(args: dict, props: dict, zarr_url: str) -> list[str]:
    """New images the task writes next to its input image."""
    if _arg(args, props, 'overwrite_input'):
        return []
    zarr_url = zarr_url.rstrip('/')
    parent, image = os.path.split(zarr_url)
    stem, ext = os.path.splitext(image)
    urls = []

    suffix = _arg(args, props, 'output_image_suffix')
    if isinstance(suffix, str) and suffix.strip():
        suffix = suffix.strip().replace(' ', '_')
        urls.append(os.path.join(parent, f'{stem}_{suffix}{ext}'))

    template = _arg(args, props, 'output_image_name')
    if isinstance(template, str) and template:
        name = _fill(template, image_name=image)
        if name:
            urls.append(os.path.join(parent, name))
    return urls


def reported_image_urls(out_json: str, zarr_url: str) -> list[str]:
    """Images listed in the task's ``image_list_updates``, except the input."""
    try:
        with open(out_json) as f:
            metadata = json.load(f) or {}
    except (OSError, ValueError):
        return []
    if not isinstance(metadata, dict):
        return []
    urls = []
    for update in metadata.get('image_list_updates') or []:
        url = update.get('zarr_url') if isinstance(update, dict) else None
        if url and os.path.normpath(url) != os.path.normpath(zarr_url):
            urls.append(url)
    return urls
