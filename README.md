# napari-workflow-tasks

Run [Fractal](https://fractal-analytics-platform.github.io/) tasks interactively on OME-Zarr data in napari.

The plugin reads a Fractal task package's manifest (`__FRACTAL_MANIFEST__.json`), builds a parameter form for each task, and runs the task on the OME-Zarr that is open in napari. Each task package runs in its own isolated [pixi](https://pixi.sh) environment, so its dependencies (e.g. Cellpose) don't need to be installed alongside napari. Results are written back into the OME-Zarr and, for supported tasks, reloaded into the viewer.

Prototype started at the [2024 OME-NGFF workflows hackathon](https://doi.org/10.37044/osf.io/5uhwz_v2) and developed further at the 2026 hackathon.

## Requirements

- [pixi](https://pixi.sh/latest/#installation) installed and available on your `PATH`. The plugin calls `pixi` to create the task environments and run tasks in them.
- Python 3.11 or newer (`requires-python = ">=3.11"`; ngio requires 3.11)
- A Qt binding for napari, e.g. PyQt5 (the widgets use [qtpy](https://github.com/spyder-ide/qtpy), so any binding qtpy supports works; the pixi workspace installs PyQt5)

## Installation

The repository is a pixi workspace that includes napari, PyQt, [napari-ome-zarr](https://github.com/ome/napari-ome-zarr), and the plugin in editable mode:

```bash
git clone https://github.com/krentzd/napari-workflow-tasks
cd napari-workflow-tasks
pixi run napari
```

The first run solves and installs the environment, which can take a few minutes.

## Usage

### 1. Open an OME-Zarr

Open an OME-Zarr image in napari and choose the **napari-ome-zarr** reader when asked.

<img width="144" alt="plugin_selection" src="https://github.com/user-attachments/assets/9a6914ab-16f7-4d3c-a042-44d8c5278eec" />

### 2. Open the plugin

**Plugins → Napari workflow tasks → Napari Fractal Task** opens the *Fractal Task Launcher* panel.

<img width="407" alt="open_plugin" src="https://github.com/user-attachments/assets/e0f92a32-e234-41ad-ab92-e9c9937100e6" />

### 3. Install a task package

In the task catalog, click **Load catalog**, find a task (search by task, package or category) and pick a version, then click **Install & add task**. The plugin downloads the package, creates its own pixi environment under `~/.napari_workflow_tasks/envs/` (set `NAPARI_WORKFLOW_TASKS_ROOT` to use another folder), checks that its tasks import, and opens the selected task in a tab.

Installing a package for the first time can take several minutes; later installs of the same version reuse the cached environment.

### 4. Pick and configure a task

Select a task under **Select task**, then click **Add task**. Each task opens in its own tab, with input fields generated from the task's argument schema (defaults pre-filled).

Only *parallel* tasks (the ones that process a single OME-Zarr image) are listed. `zarr_url` is taken from the image layer selected under **Input**, so it is not shown in the form; `channels_to_include`, `channels_to_exclude` and `measure_texture` are hidden as well. Hover over a field label to see its description.

Nested arguments (e.g. `channel` or Cellpose model parameters) get their own sub-tab inside the task tab. Enter list values as comma-separated text.

### 5. Run the task

Click **Execute task**. The plugin writes the parameters to `<task title>.json` in the task's own folder, `~/.napari_workflow_tasks/tasks/<package>/<version>/<task name>/`, and runs the task's executable in the package's pixi environment. The task's output file (`out_file.json`) goes to the same folder; both are overwritten on the next run of that task:

```bash
pixi run --manifest-path <pixi manifest> python <executable> --args-json <task title>.json --out-json out_file.json
```

The task reads from and writes back to the OME-Zarr on disk.

After **Thresholding Label Task**, **Cellpose Segmentation**, and **Cellpose SAM Segmentation**, the new label layer is reloaded into napari automatically; existing label layers are removed first. For other tasks, reopen the OME-Zarr to see the results.

<img width="1579" alt="napari_tasks_cellpose_zoom" src="https://github.com/user-attachments/assets/4b1eb82a-7e65-4b3f-9f38-9cf78e3ef878" />

### Restrict tasks to ROIs

To run tasks on part of an image only, define regions of interest and save them as an ROI table in the OME-Zarr:

1. Select the image under **Input**, click **Add Shapes Layer for ROI selection**, and draw one or more polygons on the image. If there are several Shapes layers, only the first one is used.
2. Enter a name under **ROI Table Name** (default: `Cropped_FOV_ROI_table`). Tick **Overwrite** to replace an existing table of the same name.
3. Click **Save ROIs to table**.

The 2D (y/x) bounding box of each polygon is saved as an ROI named `FOV_1`, `FOV_2`, … (using [ngio](https://github.com/fractal-analytics-platform/ngio)). You can then enter the table name in a task's ROI-table parameter (e.g. `input_ROI_table`).

## Current limitations

- Only parallel (single-image) tasks are supported; compound and non-parallel tasks are hidden.
- Results are reloaded into the viewer automatically only for the three segmentation tasks listed above.
- The napari UI is blocked while a task runs.
- Only one level of nested arguments is supported; deeper models and `object`-typed arguments without a schema reference get no input field.
- Task packages without a pixi configuration must use a Poetry-style `pyproject.toml`; packages with a pixi configuration must use the hatch build layout.

## Development

```bash
pixi run -e testing pytest
```

### Code layout

The distribution `napari-workflow-tasks` ships two Python packages under `src/`:

- `workflow_tasks_core`: everything that does not need a viewer. It finds task packages and their versions (`task_packages`, `snapshot`, `manifests`), installs each into its own pixi environment (`install`, `pixi_config`, `env_cache`, `progress`), keeps track of installed tasks (`tasks`), reads task argument schemas (`schema`), runs tasks (`runner`), works out what they wrote (`outputs`, `zarr_meta`) and builds ROI tables (`rois`). File locations are in `paths`.
- `napari_workflow_tasks`: the napari plugin only, i.e. the Qt widgets (`_widget`, `_catalog_panel`, `_schema_form`), the handling of viewer layers and `napari.yaml`. It imports `workflow_tasks_core` for everything else.

`workflow_tasks_core` must never import napari, Qt (`qtpy`, PyQt, PySide, `superqt`), `magicgui` or `napari_ome_zarr`, directly or indirectly, so that it can be used headless. `workflow_tasks_core/_tests/test_no_napari.py` checks this.

Tests live next to the code they test, in each package's `_tests/` folder.

## How to cite

```bibtex
@article{luthi20252024,
  title={2024 OME-NGFF workflows hackathon},
  author={L{\"u}thi, Joel and Albert, Marvin and Anita, Liviu and Babalola, Kola and Bennett, Davis and Bogovic, John A and Cerrone, Lorenzo and Dornier, R{\'e}my and Eglinger, Jan and Galinova, Vera and others},
  year={2025},
  publisher={OSF}
}
```

## License

MIT; see [LICENSE](LICENSE).
