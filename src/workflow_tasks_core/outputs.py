"""Work out what a task wrote from the arguments it was run with.

Fractal tasks name their outputs from their arguments, so a run's outputs
(``Task.outputs``) are exactly those, not a guess:

- tables: ``table_name`` / ``output_table_name``, also inside argument
  models (e.g. ``create_masking_roi_table``);
- label images: ``label_name`` / ``output_label_name``, where
  ``{channel_identifier}`` is the channel the task segmented
  (``channel.identifier``, or the first of ``channels.identifiers``);
- new images: ``output_image_suffix`` (written next to the input as
  ``<image>_<suffix>``) and ``output_image_name`` templates that only use
  ``{image_name}`` (besides those the task reports in its
  ``image_list_updates``).

``args`` are the arguments sent to the task. Arguments left at their default
are not sent, so the schema default (in ``props``) is used for them.
"""

from __future__ import annotations

import os
import string
from typing import Any, Optional

from .schema import pick_variant, resolve, schema_variants, with_defs

LABEL_NAME_ARGS = ('label_name', 'output_label_name')
# Arguments naming a table the task writes (also inside argument models,
# e.g. create_masking_roi_table.table_name)
TABLE_NAME_ARGS = ('table_name', 'output_table_name')
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


def _table_names(value: dict, schema: dict, defs: dict, fill: dict,
                 names: list) -> None:
    schema = resolve(schema, defs)
    if schema_variants(schema):
        schema = pick_variant(value, schema, schema_variants(schema), defs) or {}
    props = schema.get('properties') or {}
    for key in list(props) + [k for k in value if k not in props]:
        sub = props.get(key, {})
        item = value[key] if key in value else resolve(sub, defs).get('default')
        if key in TABLE_NAME_ARGS and isinstance(item, str) and item:
            name = _fill(item, **fill)
            if name and name not in names:
                names.append(name)
        elif isinstance(item, dict) and sub:
            _table_names(item, sub, defs, fill, names)


def expected_table_names(args: dict, schema: dict) -> list[str]:
    """Tables the task writes into its input image: ``table_name`` /
    ``output_table_name`` arguments, also inside argument models (with the
    defaults of the model variant chosen), where ``{label_name}`` and
    ``{channel_identifier}`` are filled in. ``schema`` is the task's whole
    args schema (with ``$defs``)."""
    schema = with_defs(schema)
    fill = {}
    channel = channel_identifier(args)
    if channel:
        fill['channel_identifier'] = channel
    labels = expected_label_names(args, schema['properties'])
    if labels:
        fill['label_name'] = labels[0]
    names: list[str] = []
    _table_names(args, schema, schema.get('$defs') or {}, fill, names)
    return names


def expected_image_urls(args: dict, props: dict, zarr_url: str) -> list[str]:
    """New images the task writes next to its input image."""
    if _arg(args, props, 'overwrite_input'):
        return []
    zarr_url = zarr_url.rstrip('/')
    image = os.path.split(zarr_url)[1]
    # the parent with the separator the input used: os.path.join would put
    # a backslash into a forward-slash URL on Windows
    prefix = zarr_url[:len(zarr_url) - len(image)]
    stem, ext = os.path.splitext(image)
    urls = []

    suffix = _arg(args, props, 'output_image_suffix')
    if isinstance(suffix, str) and suffix.strip():
        suffix = suffix.strip().replace(' ', '_')
        urls.append(f'{prefix}{stem}_{suffix}{ext}')

    template = _arg(args, props, 'output_image_name')
    if isinstance(template, str) and template:
        name = _fill(template, image_name=image)
        if name:
            urls.append(prefix + name)
    return urls


def rewritten_inputs(
    image_list_updates: list[dict], input_urls: list[str]
) -> list[str]:
    """Inputs that the task lists in its ``image_list_updates``: it
    rewrote them in place (e.g. Apply Registration to Image with
    ``overwrite_input``), so data read before the run is out of date."""
    updated = {os.path.normpath(u['zarr_url'])
               for u in image_list_updates if u.get('zarr_url')}
    return [url for url in input_urls if os.path.normpath(url) in updated]
