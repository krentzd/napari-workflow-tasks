# napari-fractal-tasks

A [napari](https://napari.org/stable/index.html) plugin to install and run [Fractal](https://fractal-analytics-platform.github.io/) tasks locally on [OME-Zarr](https://ngff.openmicroscopy.org/) images. Tasks can either be installed from the [Fractal task catalog]( https://fractal-analytics-platform.github.io/fractal_tasks/) or from tarball files (`.tar.gz`). 

![width=200](https://raw.githubusercontent.com/krentzd/napari-fractal-tasks/main/resources/images/napari-plugin-screenshot.png)



## Usage 

### Installation
The napari-fractal-tasks plugin requires [pixi](https://pixi.prefix.dev/latest/), which installs each task package in its own environment. Install pixi first; it is not installed together with the plugin.

To add the plugin to an existing napari, install `napari-fractal-tasks` from napari's plugin manager (`Plugins > Install/Uninstall Plugins`) or with pip:

```bash
pip install napari-fractal-tasks
```

To run the plugin from its source code instead, open the terminal and run the following commands:

```bash
git clone https://github.com/krentzd/napari-fractal-tasks
cd napari-fractal-tasks
pixi run napari
```

### Running Fractal tasks

[![Watch the video](https://raw.githubusercontent.com/krentzd/napari-fractal-tasks/main/resources/images/video-screenshot.png)](https://youtu.be/Dy_UNt88HGA)

1. **Load task packages** either from the [Fractal task catalog]( https://fractal-analytics-platform.github.io/fractal_tasks/), or upload your own local task as a `.tar.gz` file by clicking on `Manage packages`.
2. **Open an OME-Zarr file** using the [napari-ome-zarr](https://github.com/ome/napari-ome-zarr) plugin by drag-and-drop. 
2. **Select and add a task** from the `Task` widget. Run your task on one image, several images or a whole plate, and interatively tune parameters of the task.
3. **Explore different parameter settings** interactively in the napari viewer. Results are written back to the OME-Zarr file and opened in the viewer once the task finishes.
4. **Export** the parameters and directly load them into the [fractal-server](https://github.com/fractal-analytics-platform/fractal-server).

### Tips
- Parameter settings exported via the fractal-server can be directly imported for a given task.
- Workflows can also run headless e.g. in a Jupyter notebook (see [`examples`](https://github.com/krentzd/napari-fractal-tasks/tree/main/examples)).

- Restrict a task to a region of interest (ROI):
  1. Click **Add Shapes Layer for ROI selection** and draw a polygon.
  2. Name the table under **ROI Table Name** (select **Overwrite** to replace an existing one).
  3. Click **Save ROIs to table**.

### Notes:
- Requires **Python 3.11+** or higher.
- **First install of a task package** can take several minutes. Installing the same version again reuses the cached environment and takes about a second.
- **Disk space:** each (package, version) is a separate environment. Everything lives in `~/.napari_fractal_tasks`.

## Test data
- **Segmentation:** `20200812-CardiomyocyteDifferentiation14-Cycle1_mip.zarr` ([Zenodo](https://zenodo.org/records/10424292)).
- **Registration:** `220605_151046_mip.zarr` ([Zenodo](https://zenodo.org/records/13982701)), a two-cycle multiplexed plate (DAPI is `A04_C01` in both cycles).

## Current limitations
Converter tasks are not supported. The data already needs to be in OME-Zarr file format.

## How to cite

The prototype was started at the [2024 OME-NGFF workflows hackathon](https://doi.org/10.37044/osf.io/5uhwz_v2) and developed further at the 2025 and 2026 hackathons.

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
