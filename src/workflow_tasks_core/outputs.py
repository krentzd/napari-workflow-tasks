"""Work out what a task wrote from the arguments it was run with.

Fractal tasks name their outputs from their arguments, so the plugin can
show exactly those outputs after a run instead of guessing:

- tables: ``table_name`` / ``output_table_name``, also inside argument
  models (e.g. ``create_masking_roi_table``);
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
from dataclasses import dataclass
from typing import Any, Optional

from .schema import pick_variant, resolve, schema_variants, with_defs
from .zarr_meta import list_labels

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


def read_image_list_updates(out_json: str) -> list[dict]:
    """The ``image_list_updates`` in the task's ``--out-json`` file."""
    try:
        with open(out_json) as f:
            metadata = json.load(f) or {}
    except (OSError, ValueError):
        return []
    if not isinstance(metadata, dict):
        return []
    return [u for u in metadata.get('image_list_updates') or []
            if isinstance(u, dict)]


def reported_image_urls(out_json: str, zarr_url: str) -> list[str]:
    """Images listed in the task's ``image_list_updates``, except the input."""
    urls = []
    for update in read_image_list_updates(out_json):
        url = update.get('zarr_url')
        if url and os.path.normpath(url) != os.path.normpath(zarr_url):
            urls.append(url)
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


# --------------------------------------------------------------------------
# Outputs found on disk after a run
# --------------------------------------------------------------------------


@dataclass
class TaskOutputs:
    """What a run on one image wrote, going by its arguments and the disk."""

    label_names: set[str]  # label images in the input image to show
    image_urls: list[str]  # images to open
    missing: list[str]     # expected outputs that are not on disk, as text
    input_rewritten: bool = False  # the task rewrote the input image


def find_task_outputs(
    args: dict, props: dict, out_json: str, labels_before: set[str]
) -> TaskOutputs:
    """Outputs of a run on ``args['zarr_url']``: the labels named by its
    arguments or new since ``labels_before``, the images it reports in its
    ``--out-json`` file and the new images its arguments name."""
    zarr_url = args['zarr_url']
    labels_on_disk = set(list_labels(zarr_url))
    expected_labels = expected_label_names(args, props)
    label_names = (labels_on_disk & set(expected_labels)) | (
        labels_on_disk - labels_before
    )
    missing = [f"label '{n}' in {zarr_url}"
               for n in expected_labels if n not in labels_on_disk]

    image_urls = reported_image_urls(out_json, zarr_url)
    for url in expected_image_urls(args, props, zarr_url):
        if os.path.isdir(url):
            if url not in image_urls:
                image_urls.append(url)
        elif url not in image_urls:
            missing.append(f"image {url}")
    input_rewritten = bool(rewritten_inputs(
        read_image_list_updates(out_json), [zarr_url]))
    return TaskOutputs(label_names, image_urls, missing, input_rewritten)


def new_label_names(
    zarr_url: str, labels_before: set[str], args: dict, props: dict
) -> set[str]:
    """Label images of ``zarr_url`` to show after a run: those new since
    ``labels_before`` and those named by the arguments."""
    on_disk = set(list_labels(zarr_url))
    names = on_disk - labels_before
    names |= on_disk & set(expected_label_names(
        {**args, 'zarr_url': zarr_url}, props))
    return names


def new_image_urls(
    image_list_updates: list[dict], input_urls: list[str]
) -> list[str]:
    """Images in a run's ``image_list_updates`` that are not its inputs."""
    input_set = {os.path.normpath(u) for u in input_urls}
    new_urls = []
    for update in image_list_updates:
        url = update.get('zarr_url')
        if url and os.path.normpath(url) not in input_set and url not in new_urls:
            new_urls.append(url)
    return new_urls
