# napari-fractal-tasks
Interact with fractal tasks in napari

Prototype developed at the 2024 

## Installation

1. Set up an environment with napari and the plugin installed (mamba example):
```
git clone https://github.com/krentzd/napari-workflow-tasks/
cd napari-workflow-tasks
mamba create -n napari-fractal-task python=3.11 -y
mamba activate napari-fractal-task
mamba install napari pyqt
pip install -e .
```

2. Install napari-ome-zarr (to be able to open OME-Zarrs in napari):
```
pip install napari-ome-zarr
```

3. Install the task packages you want to use:
```
pip install "fractal-tasks-core[fractal-tasks]"
cd ../my_task_package_name
pip install -e .
```

## Usage
1. Open an OME-Zarr in napari (select the napari-ome-zarr plugin to open it)

2. Open the napari Fractal task plugin

3. Add a Fractal workflow package by providing the Fractal manifest json file. The task package needs to be installed in your Python environment that runs napari

4. Select a task from the dropdown

5. In the new tab, fill in all relevant parameters, then click `Execute task`.

6. The plugin now runs the task in the background by loading the data from the on-disk OME-Zarr and saving the results back into that OME-Zarr. Based on a heuristic, it tries to load the result back into napari to show to the user.


## Scope limits

- Currently only works for Segmentation, Image Processing(?) and Measurement tasks
- Requires tasks packages to be installed in the same environment as napari
