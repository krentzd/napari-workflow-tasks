# napari-workflow-tasks

**Interatively run Fractal tasks in Napari: Pick a task, tune its parameters, run it, explore the result.**

<table>
  <tr>
    <td width="30%"><img alt="Opening the plugin from the napari Plugins menu" src="https://github.com/user-attachments/assets/e0f92a32-e234-41ad-ab92-e9c9937100e6" /></td>
    <td width="70%"><img alt="Cellpose segmentation run from the plugin, shown in napari" src="https://github.com/user-attachments/assets/4b1eb82a-7e65-4b3f-9f38-9cf78e3ef878" /></td>
  </tr>
  <tr>
    <td align="center"><em>Plugins → Napari workflow tasks</em></td>
    <td align="center"><em>Cellpose segmentation of an OME-Zarr image, run from the plugin</em></td>
  </tr>
</table>

[TODO add video]

### Plugin Description
Fractal tasks are modular and interoperable processing units that handle data in OME-Zarr containers. Each task is an executable that runs on a single OME-Zarr image or a collection of OME-Zarr images. In this napari plugin, we determine the OME-Zarrs to be processed by manual user selection of the images/label layers open in the napari viewer for interactive exploration of tasks and their parameters. All tasks load data from an OME-Zarr on disk and store their processing results in an OME-Zarr (the same or a new one) on disk again. Each task package gets its own [pixi](https://pixi.sh) environment, so its dependencies (PyTorch, Cellpose, …) never touch your base napari environment.

## What you can do
1. **Find** any public task from the [Fractal task catalog]( https://fractal-analytics-platform.github.io/fractal_tasks/), or upload your own local task.
2. **Install** its package into an isolated pixi environment, with one click.
3. **Run** it on one image, several images or a whole plate, and *interatively* tune parameters of the task.
4. **Explore** the results: new label images and new images are opened in the viewer; images a task rewrote are reloaded.
5. **Import/export** the parameters (compatible with fractal-server).

The same works headless e.g. in a notebook (see [`examples/`](https://github.com/krentzd/napari-workflow-tasks/tree/main/examples)).

## Install
You need [pixi](https://pixi.sh/latest/#installation) (on your `PATH` or in `~/.pixi/bin`). 

```bash
git clone https://github.com/krentzd/napari-workflow-tasks
cd napari-workflow-tasks
pixi run napari
```

### Notes:
- Requires **Python 3.11+** or higher.
- **First install of a task package** can take several minutes. Installing the same version again reuses the cached environment and takes about a second.
- **Disk space:** each (package, version) is a separate environment. Everything lives in `~/.napari_workflow_tasks`; set `NAPARI_WORKFLOW_TASKS_ROOT` to use another folder.

## Useful tips
### Restrict a task to a region of interest
1. Click **Add Shapes Layer for ROI selection** and draw polygon(s).
2. Name the table under **ROI Table Name** (default `Cropped_FOV_ROI_table`); tick **Overwrite** to replace an existing one.
3. Click **Save ROIs to table**.

## Test data
- **Segmentation:** `20200812-CardiomyocyteDifferentiation14-Cycle1_mip.zarr` ([Zenodo](https://zenodo.org/records/10424292)), well B03.
- **Registration:** `220605_151046_mip.zarr`, a two-cycle multiplexed plate; DAPI is `A04_C01` in both cycles.

## Current limitations
Converter tasks are not supported. The data already needs to be in OME-Zarr file format.

## OME-NGFF workflows hackathons
The prototype was started at the [2024 OME-NGFF workflows hackathon](https://doi.org/10.37044/osf.io/5uhwz_v2) and developed further at the 2026 hackathon.

### How to cite
```bibtex
@article{luthi20252024,
  title={2024 OME-NGFF workflows hackathon},
  author={L{\"u}thi, Joel and Albert, Marvin and Anita, Liviu and Babalola, Kola and Bennett, Davis and Bogovic, John A and Cerrone, Lorenzo and Dornier, R{\'e}my and Eglinger, Jan and Galinova, Vera and others},
  year={2025},
  publisher={OSF}
}
```

## License
The code and text are under the [MIT license](LICENSE).
