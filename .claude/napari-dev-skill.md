---
name: napari-dev
description: Expert guidance for napari plugin development: npe2 plugins, magicgui widgets, image processing workflows, and pytest-napari testing — always in the context of the user's own plugin codebase. Includes Fractal framework task authoring.
---

# napari-dev skill

You are an expert napari plugin developer and Fractal task author. The user is always working inside their own plugin/task codebase. Apply all guidance in that context — read existing code before proposing changes, follow conventions already present, and never scaffold generic boilerplate when the project already has structure.

## Core assumptions
- The project uses **npe2** (not the legacy `napari_plugin_engine` hooks). If you see `napari.yaml` or `pyproject.toml` with `[tool.napari]`, that is the manifest.
- Python ≥ 3.9 (CI typically targets 3.11 and 3.12), napari ≥ 0.4.18.
- The scientific Python stack is available: numpy, scipy, scikit-image. Additional domain tools (cellpose, stardist, ome-zarr) may be present — check imports before assuming.
- Tests use **pytest** with the `make_napari_viewer` fixture from `napari.utils._testing` or `pytest-napari`.
- Fractal tasks use **fractal-task-tools** and **fractal-tasks-core** for task models and manifest generation.

---

## 1. Plugin architecture (npe2)

### Manifest (`napari.yaml` or `pyproject.toml`)
Every contribution point must be declared here. Common ones:
```yaml
name: my-plugin
display_name: My Plugin
contributions:
  commands:
    - id: my-plugin.do_thing
      python_name: my_plugin._widget:DoThingWidget
      title: Do Thing
  widgets:
    - command: my-plugin.do_thing
      display_name: Do Thing
      autogenerate: false
  readers:
    - command: my-plugin.read_files
      filename_extensions: [".tif", ".tiff"]
      accepts_directories: false
  writers:
    - command: my-plugin.write_labels
      layer_types: ["labels"]
      filename_extensions: [".tif"]
  sample_data:
    - command: my-plugin.make_sample
      display_name: Sample Data
      key: sample
```

### Entry point (pyproject.toml)
```toml
[project.entry-points."napari.manifest"]
my-plugin = "my_plugin:napari.yaml"
```
The YAML path is relative to the package root.

### Contribution point rules
- **commands** are the atomic unit — every other contribution references a command id.
- `python_name` must be importable at the time napari loads the manifest; use lazy imports inside the function/class body to keep startup fast.
- Prefer `autogenerate: true` only for simple `@magic_factory` widgets; hand-craft the widget class for anything with state or non-trivial UI.

---

## 2. Widgets & UI

### magicgui
Use `@magic_factory` for stateless, function-style widgets:
```python
from magicgui import magic_factory
import napari

@magic_factory(auto_call=False)
def threshold_widget(
    image: "napari.types.ImageData",
    threshold: float = 0.5,
) -> "napari.types.LabelsData":
    return (image > threshold).astype(int)
```
- Annotate with `napari.types.*Data` or `napari.layers.*` to get layer-aware dropdowns automatically.
- `auto_call=True` re-runs on every parameter change — use only for cheap operations.

### QWidget subclass (preferred for stateful widgets)
```python
from qtpy.QtWidgets import QWidget, QVBoxLayout, QPushButton
import napari

class MyWidget(QWidget):
    def __init__(self, viewer: napari.Viewer, parent=None):
        super().__init__(parent)
        self._viewer = viewer
        btn = QPushButton("Run")
        btn.clicked.connect(self._run)
        layout = QVBoxLayout()
        layout.addWidget(btn)
        self.setLayout(layout)

    def _run(self):
        # operate on self._viewer.layers
        pass
```
- Always accept `viewer: napari.Viewer` as the first argument — napari injects it.
- Use `qtpy` (not `PyQt5`/`PySide2` directly) for Qt-version portability.
- Connect to viewer events via `self._viewer.layers.events.inserted.connect(...)` etc.

### Layer events cheatsheet
```python
viewer.layers.events.inserted.connect(on_layer_added)
viewer.layers.selection.events.changed.connect(on_selection_changed)
layer.events.data.connect(on_data_changed)
layer.events.visible.connect(on_visibility_changed)
```
Always disconnect in `closeEvent` or use weak references to avoid memory leaks.

---

## 3. Image processing workflows

### Working with layer data
```python
# Get data from a layer
image_layer = viewer.layers["my-image"]  # by name
data = image_layer.data  # numpy or dask array

# Add a result layer
viewer.add_labels(result, name="segmentation")
viewer.add_image(filtered, name="filtered", colormap="magma")
```

### Dask arrays
- Prefer lazy computation for large data; only materialise when writing or displaying.
- Use `dask.array.map_blocks` for chunk-wise processing:
```python
import dask.array as da
result = da.map_blocks(my_func, data, dtype=data.dtype)
```

### Dimension conventions
- napari uses `(t, z, y, x)` or `(z, y, x)` axis order.
- `layer.data.shape` gives the full shape; `viewer.dims.current_step` gives the current slice index per axis.
- For anisotropic data, set `scale` on the layer: `viewer.add_image(data, scale=(z_res, y_res, x_res))`.

### Common patterns
```python
# Update existing layer in-place (avoids flicker)
if "result" in viewer.layers:
    viewer.layers["result"].data = new_data
else:
    viewer.add_labels(new_data, name="result")
```

---

## 4. Testing (napari)

### Fixtures
```python
import pytest
import numpy as np

def test_widget(make_napari_viewer):
    viewer = make_napari_viewer()
    viewer.add_image(np.random.rand(64, 64), name="test")
    from my_plugin._widget import MyWidget
    widget = MyWidget(viewer)
    widget._run()
    assert "result" in viewer.layers
```

### Headless / CI setup
- Set `DISPLAY=:0` or use `xvfb-run` on Linux CI.
- Add to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
addopts = "--tb=short"
```
- Use `pytest-qt` for Qt event loop integration; avoid `app.exec()` in tests.

### Testing readers/writers
```python
def test_reader(tmp_path):
    from my_plugin._reader import napari_get_reader
    test_file = tmp_path / "test.tif"
    tifffile.imwrite(str(test_file), np.zeros((10, 10), dtype=np.uint8))
    reader = napari_get_reader(str(test_file))
    assert reader is not None
    layer_data = reader(str(test_file))
    assert len(layer_data) == 1
```

---

## 5. Fractal framework

Fractal is an open-source platform (developed at BioVisionCenter, University of Zurich) for processing bioimaging data — especially OME-Zarr HCS plates — at scale, locally or on HPC/SLURM clusters. Tasks are Python executables that follow the Fractal task spec and are registered via a `__FRACTAL_MANIFEST__.json`.

### Five task types

| Type | Reserved args | Use case |
|---|---|---|
| **Parallel** | `zarr_url: str` | Process one OME-Zarr per job, run in parallel |
| **Non-parallel** | `zarr_urls: list[str]`, `zarr_dir: str` | Aggregate across many images, one job |
| **Compound** | init: `zarr_urls`, `zarr_dir`; compute: `zarr_url`, `init_args` | Custom parallelization (e.g. per-acquisition) |
| **Non-parallel converter** | `zarr_dir: str` | Convert raw data → OME-Zarr (no input images yet) |
| **Compound converter** | init: `zarr_dir`; compute: `zarr_url`, `init_args` | Compound version of converter |

Reserved args (`zarr_url`, `zarr_urls`, `zarr_dir`, `init_args`) are injected by Fractal server — the user cannot set them directly.

### Task function signatures

```python
# Parallel task
def my_parallel_task(
    zarr_url: str,           # reserved — path to one OME-Zarr image
    threshold: float = 0.5,  # user-settable
) -> None:
    ...

# Non-parallel task
def my_non_parallel_task(
    zarr_urls: list[str],   # reserved
    zarr_dir: str,          # reserved — base dir for new OME-Zarrs
    channel_label: str = "DAPI",
) -> dict:
    ...
    return {"image_list_updates": [{"zarr_url": new_url, "attributes": {...}}]}

# Compound task — init
def my_compound_init(
    zarr_urls: list[str],
    zarr_dir: str,
    acquisitions: list[int],
) -> dict[str, list[dict]]:
    return {
        "parallelization_list": [
            {"zarr_url": url, "init_args": {"acquisition": acq}}
            for url, acq in zip(zarr_urls, acquisitions)
        ]
    }

# Compound task — compute
def my_compound_compute(
    zarr_url: str,
    init_args: dict,         # reserved — dict from the parallelization list
    model_path: str = "",
) -> None:
    acquisition = init_args["acquisition"]
    ...
```

### Metadata return values
All tasks except compound-init can return `image_list_updates` and/or `image_list_removals`:
```python
# Add/update an image in the image list
return {
    "image_list_updates": [
        {
            "zarr_url": "/path/to/plate.zarr/B/03/0_processed",
            "origin": "/path/to/plate.zarr/B/03/0",   # optional
            "attributes": {"plate": "plate_name", "well": "B03"},
            "types": {"is_3D": True},
        }
    ]
}

# Remove an image from the image list
return {"image_list_removals": ["/path/to/plate.zarr/B/03/0"]}
```
All returned values **must be JSON-serializable** — they are written to disk by Fractal server.

### Task list (`src/{package}/dev/task_list.py`)
This is where tasks are registered. It drives manifest generation.
```python
from fractal_tasks_core.dev.task_models import (
    NonParallelTask, ParallelTask, CompoundTask,
    ConverterCompoundTask, ConverterNonParallelTask,
)

TASK_LIST = [
    NonParallelTask(
        name="Create OME-Zarr Structure",
        executable="create_ome_zarr.py",
        meta={"cpus_per_task": 1, "mem": 4000},
        category="Conversion",
        modality="HCS",
        tags=["HCS", "OME-Zarr"],
    ),
    ParallelTask(
        name="Segment Nuclei",
        executable="segment_nuclei.py",
        meta={"cpus_per_task": 4, "mem": 16000},
        category="Segmentation",
        tags=["Deep Learning", "Nuclei"],
    ),
    CompoundTask(
        name="Calculate Multiplexing Registration",
        executable_init="registration_init.py",
        executable="registration_compute.py",
        meta_init={"cpus_per_task": 1, "mem": 4000},
        meta={"cpus_per_task": 2, "mem": 12000},
        category="Registration",
    ),
]
```

**Allowed categories:** `Conversion`, `Image Processing`, `Segmentation`, `Registration`, `Measurement`  
**Allowed modalities:** `HCS`, `lightsheet`, `EM`  
**Tags:** arbitrary strings — increase findability on the Fractal task page.  
**`meta` keys:** `cpus_per_task`, `mem` (MB) — used for SLURM resource allocation.

### Manifest generation
After adding a task or changing its arguments/docstring:
```bash
fractal-manifest create --package my_package_name
git add src/my_package_name/__FRACTAL_MANIFEST__.json
git commit -m 'Update __FRACTAL_MANIFEST__.json'
```
Never hand-edit `__FRACTAL_MANIFEST__.json` — it is auto-generated.  
CI will fail if the manifest is stale.

Validate the manifest by uploading `__FRACTAL_MANIFEST__.json` to the [Fractal Web Sandbox](https://fractal-analytics-platform.github.io/fractal-web/sandbox/#task-manifest).

### Package layout (fractal-tasks-template conventions)
```
src/
  my_package/
    __init__.py
    __FRACTAL_MANIFEST__.json   ← auto-generated, commit to git
    dev/
      task_list.py              ← define TASK_LIST here
    tasks/
      my_parallel_task.py       ← one file per task executable
      my_non_parallel_task.py
tests/
  test_tasks.py
  test_manifest.py              ← verifies manifest is not stale
pyproject.toml
```

### Building and collecting
```bash
# Build the wheel
hatch build

# Collect tasks on a Fractal server (via fractal-client)
fractal task collect /path/to/dist/my_package-0.1.0-py3-none-any.whl
# or via PyPI:
fractal task collect my_package
```

### Testing Fractal tasks
```python
def test_parallel_task(tmp_path):
    zarr_url = str(tmp_path / "test.zarr")
    # ... setup minimal OME-Zarr ...
    from my_package.tasks.my_parallel_task import my_parallel_task
    my_parallel_task(zarr_url=zarr_url, threshold=0.5)
    assert (tmp_path / "test.zarr" / "labels").exists()

def test_manifest_is_up_to_date():
    import json
    from pathlib import Path
    import my_package
    manifest_path = Path(my_package.__file__).parent / "__FRACTAL_MANIFEST__.json"
    # Compare against freshly generated manifest to check for staleness
```

Run tests:
```bash
python -m pytest --log-cli-level info -s
```

---

## 6. OME-Zarr / NGFF

OME-Zarr (also called NGFF — Next Generation File Format) is the on-disk format used by Fractal and readable by napari. It is a chunked, cloud-optimised hierarchy stored as a Zarr group tree with JSON metadata in `.zattrs` files.

### Install
```bash
pip install ome-zarr zarr dask
```
Note: `ome-zarr` ≥ 0.9 defaults to **OME-Zarr v0.5** (zarr v3). Not all tools support v0.5 yet. To write v0.4 for maximum compatibility:
```python
write_image(..., fmt=CurrentFormat())  # v0.4
```

### Core concepts

**Zarr group tree for a single image:**
```
my_image.zarr/          ← root group (.zattrs contains "multiscales")
  0/                    ← full-resolution array
  1/                    ← 2× downsampled
  2/                    ← 4× downsampled
  labels/               ← group (.zattrs lists label names)
    nuclei/             ← label group (.zattrs has "image-label" + "multiscales")
      0/
      1/
```

**HCS plate tree:**
```
plate.zarr/             ← root (.zattrs has "plate" key)
  B/                    ← row
    03/                 ← column → this is a "well" (.zattrs has "well" key)
      0/                ← acquisition index → this is an OME-Zarr image
        0/              ← full-res array
        1/
        labels/
```

### Reading OME-Zarr

```python
import zarr
import dask.array as da
from ome_zarr.io import parse_url
from ome_zarr.reader import Reader

# Open a local or remote (S3, GCS) OME-Zarr
store = parse_url("/path/to/image.zarr", mode="r").store
root = zarr.open_group(store, mode="r")

# Read metadata
multiscales = root.attrs["multiscales"][0]
axes = [ax["name"] for ax in multiscales["axes"]]  # e.g. ["t","c","z","y","x"]

# Read image data as dask arrays (lazy, chunked)
pyramid = [da.from_zarr(root[str(level)]) for level in range(len(multiscales["datasets"]))]
full_res = pyramid[0]  # shape e.g. (1, 2, 10, 512, 512) for tczyx

# Read pixel size from coordinateTransformations
scale_meta = multiscales["datasets"][0]["coordinateTransformations"]
scale = next(t["scale"] for t in scale_meta if t["type"] == "scale")
# scale is a list matching axes order, e.g. [1.0, 1.0, 0.5, 0.25, 0.25] (tczyx in µm)

# Read labels
labels_grp = root["labels"]["nuclei"]
label_pyramid = [da.from_zarr(labels_grp[str(i)]) for i in range(3)]

# Using the high-level Reader API
reader = Reader(parse_url("/path/to/image.zarr"))
nodes = list(reader())
image_node = nodes[0]
dask_data = image_node.data  # list of dask arrays (pyramid levels)
```

### Writing OME-Zarr

```python
import numpy as np
import zarr
from ome_zarr.io import parse_url
from ome_zarr.writer import write_image, write_labels, write_multiscale

# --- Write a new image ---
store = parse_url("/path/to/output.zarr", mode="w").store
root = zarr.open_group(store, mode="w")

data = np.random.randint(0, 255, (2, 10, 512, 512), dtype=np.uint8)  # czyx

write_image(
    image=data,
    group=root,
    axes="czyx",                              # axis string or list of dicts
    storage_options={"chunks": (1, 1, 512, 512)},
    scaler=None,                              # None = default 2× XY downsampling
)

# Add channel rendering metadata
root.attrs["omero"] = {
    "channels": [
        {"label": "DAPI",  "color": "0000FF", "active": True,
         "window": {"start": 0, "end": 1000}},
        {"label": "GFP",   "color": "00FF00", "active": True,
         "window": {"start": 0, "end": 500}},
    ]
}

# --- Write labels to the same image ---
labels_grp = root.require_group("labels")
labels_grp.attrs["labels"] = ["nuclei"]
label_grp = labels_grp.require_group("nuclei")
label_grp.attrs["image-label"] = {
    "colors": [
        {"label-value": 1, "rgba": [255, 0, 0, 255]},
        {"label-value": 2, "rgba": [0, 255, 0, 255]},
    ]
}
label_data = np.zeros((10, 512, 512), dtype=np.int32)  # zyx
write_image(label_data, label_grp, axes="zyx")

# --- Write a pre-built pyramid ---
pyramid = [data, data[:, :, ::2, ::2], data[:, :, ::4, ::4]]
write_multiscale(
    pyramid=pyramid,
    group=root,
    axes=[
        {"name": "c", "type": "channel"},
        {"name": "z", "type": "space", "unit": "micrometer"},
        {"name": "y", "type": "space", "unit": "micrometer"},
        {"name": "x", "type": "space", "unit": "micrometer"},
    ],
    coordinate_transformations=[
        [{"type": "scale", "scale": [1.0, 0.5, 0.25, 0.25]}],
        [{"type": "scale", "scale": [1.0, 0.5, 0.5,  0.5 ]}],
        [{"type": "scale", "scale": [1.0, 0.5, 1.0,  1.0 ]}],
    ],
)
```

### HCS plate reading

```python
import zarr
from ome_zarr.io import parse_url

store = parse_url("/path/to/plate.zarr", mode="r").store
plate = zarr.open_group(store, mode="r")

plate_meta = plate.attrs["plate"]
rows    = [r["name"] for r in plate_meta["rows"]]     # e.g. ["B", "C"]
columns = [c["name"] for c in plate_meta["columns"]]  # e.g. ["03", "04"]
wells   = plate_meta["wells"]  # [{"path": "B/03", "rowIndex": 0, "columnIndex": 0}, ...]

# Open a specific well's acquisition
well_grp = plate["B"]["03"]
acq_path = well_grp.attrs["well"]["images"][0]["path"]  # e.g. "0"
image_grp = well_grp[acq_path]

import dask.array as da
full_res = da.from_zarr(image_grp["0"])  # first pyramid level
```

### HCS plate writing (Fractal-style)

```python
from ome_zarr.writer import write_plate_metadata, write_well_metadata

store = parse_url("/path/to/plate.zarr", mode="w").store
root = zarr.open_group(store, mode="w")

write_plate_metadata(
    group=root,
    rows=["B", "C"],
    columns=["03", "04"],
    wells=[
        {"path": "B/03", "rowIndex": 0, "columnIndex": 0},
        {"path": "B/04", "rowIndex": 0, "columnIndex": 1},
    ],
    name="MyPlate",
    field_count=1,
)

# For each well:
well_grp = root.require_group("B/03")
write_well_metadata(well_grp, images=[{"path": "0", "acquisition": 0}])

# Then write the actual image under B/03/0
image_grp = well_grp.require_group("0")
write_image(data, image_grp, axes="czyx")
```

### Axis conventions
- Full OME-Zarr axes (all optional except spatial): `t`, `c`, `z`, `y`, `x`
- Most Fractal tasks work with `(t, c, z, y, x)` or `(c, z, y, x)`.
- Axis metadata specifies `type` (`time`, `channel`, `space`) and `unit` (e.g. `micrometer`, `millisecond`).
- `coordinateTransformations` contain per-level `scale` (and optionally `translation`) arrays matching the axis order.

### Updating an existing OME-Zarr in-place (Fractal pattern)

```python
import zarr
import dask.array as da
import numpy as np

zarr_url = "/path/to/plate.zarr/B/03/0"
root = zarr.open_group(zarr_url, mode="r+")

# Read existing data
data = da.from_zarr(root["0"])  # full resolution

# Process and write result back
result = process(data.compute())
root["0"][:] = result  # overwrite in-place (same shape/dtype required)

# Or write to a new sub-path
out_grp = root.require_group("labels/my_labels")
write_image(result, out_grp, axes="zyx")
```

### Common pitfalls
- **dtype mismatch:** labels must be integer dtype (`int32` or `uint32`); images are typically `uint8`/`uint16`.
- **chunk shape:** choose chunks that match your access pattern — `(1, 1, z, y, x)` for per-z-slice reads, `(1, c, 1, y, x)` for per-channel. Mismatched chunks cause slow I/O.
- **v0.4 vs v0.5:** `ome-zarr-py` ≥ 0.9 writes v0.5 by default (zarr v3 backend). Many tools (including some napari plugins) only read v0.4. Pin or pass `fmt=CurrentFormat()` explicitly when compatibility matters.
- **Remote stores:** pass `parse_url("s3://bucket/path.zarr")` for S3. Requires `s3fs`.
- **Metadata must be written before data** when using `write_plate_metadata` / `write_well_metadata` — the server (Fractal or napari-ome-zarr) reads `.zattrs` to discover structure.

---

## 7. General rules (napari + Fractal + OME-Zarr)

- **Read before writing.** Always inspect the existing manifest, task list, widget files, and test layout before adding anything new.
- **Lazy imports.** Put heavy imports (`torch`, `cellpose`, `stardist`) inside functions, not at module top-level, to keep startup fast.
- **No blocking the GUI thread** (napari). Long-running processing goes in `napari.qt.threading.thread_worker`:
```python
from napari.qt.threading import thread_worker

@thread_worker
def run_segmentation(data):
    return heavy_computation(data)

worker = run_segmentation(data)
worker.returned.connect(on_result)
worker.start()
```
- **JSON-serializable returns** (Fractal). Task return values are written to disk — never return numpy arrays, custom objects, or non-serializable types.
- **Error messages** (napari). Surface errors via `napari.utils.notifications.show_error(msg)`, not `print`.
- **Type annotations.** Use `napari.types` annotations for magicgui dropdowns; use standard Python types with docstrings for Fractal task argument JSON Schema generation.
- **Always check axis order.** Never assume without reading `.zattrs` — `multiscales[0]["axes"]` is the ground truth.
