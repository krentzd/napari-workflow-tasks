"""
Add the layers of OME-Zarr images to a napari viewer, in particular what a
task run wrote: for the plugin's task tabs and for
``workflow_tasks_core.Task.show``.

Layers added here carry ``metadata["zarr_url"]``, so they can be used as
task inputs like layers opened from disk.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from napari.layers import Image, Labels
from napari_ome_zarr._reader import napari_get_reader

from workflow_tasks_core.runner import image_urls, short_name

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Which image a layer shows
# --------------------------------------------------------------------------


def _layer_path(layer) -> Optional[str]:
    """Path a layer was opened from, or the image the plugin added it from."""
    path = getattr(getattr(layer, "source", None), "path", None)
    return path or layer.metadata.get("zarr_url")


def layer_image_url(layer) -> Optional[str]:
    """The OME-Zarr image a layer belongs to (an image or one of its
    labels); None for a layer of a whole plate or well, or of no OME-Zarr."""
    path = _layer_path(layer)
    if not path:
        return None
    path = str(path).rstrip("/")
    if "/labels/" in path:  # label image -> its image
        path = path.split("/labels/")[0]
    return path if image_urls(path) == [path] else None


def image_layer_names(viewer) -> list[str]:
    """Names of the image layers of OME-Zarr images that have layers of
    their own (not those of a whole plate or well)."""
    return [layer.name for layer in viewer.layers
            if isinstance(layer, Image) and layer_image_url(layer) is not None]


def _same_path(a: str, b: str) -> bool:
    return os.path.normpath(a) == os.path.normpath(b)


def _shows_other_image(layer, zarr_url: str) -> bool:
    """Whether ``layer`` is known to show something else than ``zarr_url``
    (layers of unknown origin are not)."""
    if not _layer_path(layer):
        return False
    url = layer_image_url(layer)
    return url is None or not _same_path(url, zarr_url)


def label_name(layer_name: str) -> str:
    """Label image name of a label layer. napari-ome-zarr 0.10 names label
    layers by their path ("labels/labels/<name>"), 0.6 by the name only."""
    return layer_name.rsplit("/", 1)[-1]


def without_napari_suffix(layer_name: str) -> str:
    """Layer name without the " [1]", " [2]", ... napari adds to a name that
    is already taken (e.g. "org [1]" -> "org")."""
    return re.sub(r" \[\d+\]$", "", layer_name)


# --------------------------------------------------------------------------
# Adding layers
# --------------------------------------------------------------------------


def forget_cached_chunks() -> None:
    """Empty napari's dask chunk cache. Its keys depend on the zarr path, not
    on the data, so after a task rewrote an image in place (e.g. Apply
    Registration) napari would otherwise show chunks read before the run."""
    from napari.utils import resize_dask_cache

    resize_dask_cache().cache.clear()


def _read_layers(zarr_url: str):
    """(data, meta, layer type) of each layer napari-ome-zarr reads."""
    reader = napari_get_reader(zarr_url)
    if reader is None:
        logger.warning(f"Cannot read {zarr_url}")
        return
    for layer_data in reader(zarr_url):
        data = layer_data[0]
        meta = dict(layer_data[1]) if len(layer_data) > 1 else {}
        layer_type = layer_data[2] if len(layer_data) > 2 else "image"
        if layer_type == "labels" and isinstance(meta.get("name"), str):
            meta["name"] = label_name(meta["name"])
        yield data, meta, layer_type


def _with_suffix(name: str, suffix: Optional[str]) -> str:
    return f"{name} [{suffix}]" if suffix else name


def add_layers(viewer, zarr_url: str, labels: Optional[set] = None,
               images: bool = True, suffix: Optional[str] = None) -> list[str]:
    """Add layers of an OME-Zarr image; returns the names of the added
    layers.

    labels: the label layers to add (None: all of them).
    images: whether to add the image layers.
    suffix: name the layers "<name> [<suffix>]".

    Layers of this image with the same name are replaced (they show data
    from before a run).
    """
    added = []
    for data, meta, layer_type in _read_layers(zarr_url):
        if layer_type == "labels":
            if labels is not None and meta.get("name") not in labels:
                continue
        elif not images:
            continue

        name = meta.get("name")
        if isinstance(name, (list, tuple)):  # one name per channel
            meta["name"] = [_with_suffix(n, suffix) for n in name]
            names = meta["name"]
        elif name is not None:
            meta["name"] = _with_suffix(name, suffix)
            names = [meta["name"]]
        else:
            names = []

        # Label layers may have been opened under their path name
        for name in names:
            for layer in list(viewer.layers):
                if _shows_other_image(layer, zarr_url):
                    continue
                if layer.name == name or (
                    layer_type == "labels" and isinstance(layer, Labels)
                    and label_name(layer.name) == name
                ):
                    viewer.layers.remove(layer)

        new_layers = getattr(viewer, f"add_{layer_type}")(data, **meta)
        if not isinstance(new_layers, list):
            new_layers = [new_layers]
        for layer in new_layers:
            layer.visible = True
            layer.metadata["zarr_url"] = zarr_url
            added.append(layer.name)
    return added


def reload_image(viewer, zarr_url: str, labels: set = frozenset()) -> list[str]:
    """Read an image again that a task rewrote in place: replace its image
    layers and its open label layers, and add the label layers in
    ``labels``. Returns the names of the added layers."""
    old = [layer for layer in viewer.layers
           if layer_image_url(layer) is not None
           and _same_path(layer_image_url(layer), zarr_url)]
    open_labels = {label_name(without_napari_suffix(layer.name))
                   for layer in old if isinstance(layer, Labels)}
    for layer in old:
        viewer.layers.remove(layer)
    return add_layers(viewer, zarr_url, labels=open_labels | set(labels))


# --------------------------------------------------------------------------
# Showing what a run wrote
# --------------------------------------------------------------------------


@dataclass
class ShownRun:
    """What :func:`show_run` did."""

    added: list[str] = field(default_factory=list)       # layer names
    not_loaded: list[str] = field(default_factory=list)  # outputs, as text
    not_opened: int = 0  # new images left out (``max_new_images``)


def show_run(viewer, task, open_inputs: bool = True,
             max_new_images: Optional[int] = None) -> ShownRun:
    """Add what the latest run of ``task`` (a ``workflow_tasks_core.Task``)
    wrote: the labels it wrote into its input images and the new images.

    open_inputs: also add the image layers of the inputs (False when they
    are open already). Inputs the task rewrote in place are always read
    again. With several images, layers are named "<name> [<image>]".
    """
    forget_cached_chunks()
    outputs = task.outputs()
    labels: dict[str, set] = {}
    for path in outputs["labels"]:
        url, name = path.rsplit("/labels/", 1)
        labels.setdefault(url, set()).add(name)
    # The inputs, and images a compound task wrote labels into
    inputs = [url.rstrip("/") for url in task.inputs]
    inputs += [url for url in labels if url not in inputs]
    rewritten = [url.rstrip("/") for url in task.rewritten]
    new_images = outputs["images"]
    if max_new_images is not None:
        new_images = new_images[:max_new_images]
    several = len(inputs) + len(new_images) > 1

    shown = ShownRun(not_opened=len(outputs["images"]) - len(new_images))
    for url in inputs:
        names = labels.get(url, set())
        suffix = short_name(url) if several else None
        if url in rewritten:
            added = reload_image(viewer, url, names)
            suffix = None  # its layers keep their names
        elif open_inputs or names:
            added = add_layers(viewer, url, labels=names,
                               images=open_inputs, suffix=suffix)
        else:
            continue
        shown.added += added
        shown.not_loaded += [f"label '{n}' from {url}" for n in sorted(names)
                             if _with_suffix(n, suffix) not in added]
    for url in new_images:
        added = add_layers(viewer, url, suffix=short_name(url))
        shown.added += added
        if not added:
            shown.not_loaded.append(f"image {url}")
    return shown


def show_task(task, viewer=None):
    """``Task.show``: open the latest run's inputs with the labels it wrote,
    and its new images, in ``viewer`` (default: the current viewer, else a
    new one). Returns the viewer."""
    import napari

    if viewer is None:
        viewer = napari.current_viewer() or napari.Viewer()
    show_run(viewer, task, open_inputs=True)
    return viewer
