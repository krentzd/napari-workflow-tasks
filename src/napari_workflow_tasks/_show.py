"""Show the outputs of a headless run (``workflow_tasks_core.Task.show``) in
napari: the input images with only the labels the run wrote, and the new
images it created."""

from __future__ import annotations

from typing import Optional

from workflow_tasks_core.runner import short_name


def _layers(zarr_url: str):
    from napari_ome_zarr._reader import napari_get_reader

    reader = napari_get_reader(zarr_url)
    if reader is None:
        raise ValueError(f"napari-ome-zarr cannot read {zarr_url}")
    for layer_data in reader(zarr_url):
        data = layer_data[0]
        meta = dict(layer_data[1]) if len(layer_data) > 1 else {}
        layer_type = layer_data[2] if len(layer_data) > 2 else "image"
        if layer_type == "labels" and isinstance(meta.get("name"), str):
            # napari-ome-zarr 0.10 names label layers "labels/labels/<name>"
            meta["name"] = meta["name"].rsplit("/", 1)[-1]
        yield data, meta, layer_type


def _forget_cached_chunks() -> None:
    """Empty napari's dask chunk cache. Its keys depend on the zarr path,
    not on the data, so after a task rewrote an image in place (e.g. Apply
    Registration) napari would otherwise show chunks read before the run."""
    from napari.utils import resize_dask_cache

    resize_dask_cache().cache.clear()


def show_outputs(inputs: list[str], outputs: dict, viewer=None,
                 ) -> "napari.Viewer":  # noqa: F821
    """Add the run's images to ``viewer`` (default: the current viewer, else
    a new one). ``outputs``: ``Task.outputs()``. Layers of several images
    are named "<name> [<image>]". Returns the viewer."""
    import napari

    if viewer is None:
        viewer = napari.current_viewer() or napari.Viewer()
    _forget_cached_chunks()

    labels: dict[str, set] = {}
    for path in outputs.get("labels", []):
        url, name = path.rsplit("/labels/", 1)
        labels.setdefault(url, set()).add(name)
    # Inputs with labels (also images a compound task wrote labels into)
    shown: list[tuple[str, Optional[set]]] = [
        (u, labels.get(u.rstrip("/"), set())) for u in inputs]
    shown += [(u, names) for u, names in labels.items()
              if u not in {s.rstrip("/") for s, _ in shown}]
    shown += [(u, None) for u in outputs.get("images", [])]  # all labels
    several = len(shown) > 1

    for url, wanted in shown:
        for data, meta, layer_type in _layers(url):
            if layer_type == "labels" and wanted is not None \
                    and meta.get("name") not in wanted:
                continue
            if several:
                names = meta.get("name")
                suffix = f" [{short_name(url)}]"
                if isinstance(names, (list, tuple)):
                    meta["name"] = [f"{n}{suffix}" for n in names]
                elif names is not None:
                    meta["name"] = f"{names}{suffix}"
            new = getattr(viewer, f"add_{layer_type}")(data, **meta)
            for layer in new if isinstance(new, list) else [new]:
                # lets the plugin use the layer as a task input
                layer.metadata["zarr_url"] = url
    return viewer
