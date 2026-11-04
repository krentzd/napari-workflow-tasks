import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import napari
import numpy as np
from napari.layers import Shapes
from napari.qt.threading import create_worker
from napari.utils.notifications import show_error, show_info, show_warning
from napari_ome_zarr._reader import napari_get_reader
from ngio import open_ome_zarr_container
from ngio.tables import RoiTable
from qtpy.QtCore import Qt
from qtpy.QtGui import QFont, QPixmap
from qtpy.QtWidgets import (QCheckBox, QComboBox, QGroupBox,
                            QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                            QPlainTextEdit, QProgressBar, QPushButton,
                            QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from ._catalog_panel import CatalogPanel
from ._outputs import (CHANNEL_PLACEHOLDER, LABEL_NAME_ARGS,
                       expected_image_urls, expected_label_names,
                       reported_image_urls)
from ._schema_form import (ListField, SchemaError, SchemaForm, StringField,
                           _Collapsible)
from ._utils import create_roi_from_bbox
from . import catalog
from .catalog import find_pixi, task_type

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def _list_labels(zarr_url):
    """Label names of an OME-Zarr image (NGFF v0.4 or v0.5), [] if none."""
    labels_dir = Path(zarr_url) / 'labels'
    for fname, keys in (('.zattrs', ('labels',)), ('zarr.json', ('attributes', 'ome', 'labels'))):
        try:
            with open(labels_dir / fname) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for key in keys:
            data = data.get(key, {}) if isinstance(data, dict) else {}
        if isinstance(data, list):
            return data
    return []


# Fields that take channel names: offer the input image's channels
CHANNEL_FIELDS = ('identifiers', 'label', 'channel_label', 'channel_labels')


def _channel_labels(zarr_url):
    """Channel labels from the OME-Zarr "omero" metadata (v0.4 / v0.5)."""
    root = Path(zarr_url)
    for fname, keys in (('.zattrs', ('omero',)), ('zarr.json', ('attributes', 'ome', 'omero'))):
        try:
            with open(root / fname) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for key in keys:
            data = data.get(key, {}) if isinstance(data, dict) else {}
        channels = data.get('channels', []) if isinstance(data, dict) else []
        labels = [c.get('label') for c in channels if isinstance(c, dict) and c.get('label')]
        if labels:
            return labels
    return []


def _input_channel(layer_name, channels):
    """The channel an input image layer shows, or None.

    napari-ome-zarr names image layers after their channel label; layers of
    task outputs get a " [<image>]" suffix. Without channel metadata, the
    layer name is used as is."""
    if not layer_name:
        return None
    if not channels:
        return layer_name
    base = re.sub(r' \[[^\]]*\]$', '', layer_name)
    for name in (layer_name, base):
        if name in channels:
            return name
    return None


class ChannelAutofill:
    """Keep a task form's channel identifiers and output label name in step
    with the selected input channel.

    Filled, outside optional nested models: ``identifiers`` lists that need
    a value (required, or with ``minItems``), a single ``identifier`` string
    of a channel model (e.g. ``channel.identifier``; the ``identifier`` of
    other models, e.g. masking, names a label or table), and ``label_name`` /
    ``output_label_name`` when their default uses ``{channel_identifier}`` or
    they have no default ("<channel>_segmented"). The label name uses the
    first identifier. A field is only overwritten while it is empty, at its
    default, or still holds the value last filled in, so edits are kept."""

    def __init__(self, fields):
        self._identifiers = []   # ListFields (several) / StringFields (one)
        self._labels = []        # (StringField, template)
        self._filled = {}        # id(field) -> text last filled in
        self._channel = None
        self._updating = False
        self._collect(fields)
        for field in self._identifiers:
            field.changed.connect(self._fill_labels)

    def _collect(self, fields, parent=''):
        for key, field in fields.items():
            inner = getattr(field, 'form', None)
            if inner is not None:
                if not getattr(field, 'optional', False):
                    self._collect(inner.fields, parent=key)
            elif (
                key == 'identifiers' and isinstance(field, ListField)
                and (field.required or field.schema.get('minItems'))
            ):
                self._identifiers.append(field)
            elif (
                key == 'identifier' and isinstance(field, StringField)
                and field.required and 'channel' in parent.lower()
            ):
                self._identifiers.append(field)
            elif key in LABEL_NAME_ARGS and isinstance(field, StringField):
                default = field.schema.get('default')
                if default is None:
                    template = f'{CHANNEL_PLACEHOLDER}_segmented'
                elif isinstance(default, str) and CHANNEL_PLACEHOLDER in default:
                    template = default
                else:
                    continue  # fixed default name: leave it
                self._labels.append((field, template))

    def _is_auto(self, field):
        """True while the user has not typed their own value."""
        text = field.edit.text().strip()
        default = field.schema.get('default')
        if isinstance(default, list):
            default = ', '.join(map(str, default))
        auto = {'', self._filled.get(id(field))}
        if isinstance(default, str):
            auto.add(default)
        return text in auto

    def _fill(self, field, value):
        if not self._is_auto(field):
            return
        self._updating = True
        try:
            field.set_value(value)
        finally:
            self._updating = False
        self._filled[id(field)] = field.edit.text().strip()

    def _label_channel(self):
        for field in self._identifiers:
            try:
                ids = field.value()
            except SchemaError:
                continue
            if isinstance(ids, list) and ids:
                return str(ids[0])
            if isinstance(ids, str) and ids:
                return ids
        return self._channel

    def _fill_labels(self, *_):
        if self._updating:
            return
        channel = self._label_channel()
        if not channel:
            return
        for field, template in self._labels:
            self._fill(field, template.replace(CHANNEL_PLACEHOLDER, channel))

    def set_channel(self, channel):
        """Fill the fields for ``channel``; None leaves them as they are."""
        if not channel:
            return
        self._channel = channel
        for field in self._identifiers:
            self._fill(field, [channel] if isinstance(field, ListField) else channel)
        self._fill_labels()


def _label_name(layer_name):
    """Label image name of a label layer. napari-ome-zarr 0.10 names label
    layers by their path ("labels/labels/<name>"), 0.6 by the name only."""
    return layer_name.rsplit('/', 1)[-1] if isinstance(layer_name, str) else layer_name


def _stream_process(cmd):
    """Run a command, yielding its output lines; returns the exit code."""
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    for line in proc.stdout:
        yield line.rstrip('\n')
    return proc.wait()


def wipe_cache():
    from napari.utils import resize_dask_cache
    cache = resize_dask_cache()
    cache_bytes = cache.cache.available_bytes
    cache = resize_dask_cache(nbytes=0)
    cache = resize_dask_cache(nbytes=cache_bytes)

def abspath(
    root,
    relpath
):

    root = Path(root)
    if root.is_dir():
        path = root/relpath
    else:
        path = root.parent/relpath
    return str(path.absolute())

class FractalTaskManager:
    # Manage tasks by keeping track of what each tab contains and what executable it links to
    def __init__(self):
        self.tasks = dict()

    def add_task(
        self,
        name,
        package_name,
        parent_dir,
        executable_parallel,
        properties,
        defs,
        required,
        type,
        title,
        base_name=None,
        version=None,
        docs_info=None,
        docs_link=None,
    ):
        # `name` is the unique key (e.g. "Cellpose Segmentation
        # [fractal-tasks-core==1.4.0]"); `base_name` is the manifest name.

        task_dict = dict(
            title=title,
            base_name=base_name or name,
            version=version,
            docs_info=docs_info,
            docs_link=docs_link,
            package_name=package_name,
            parent_dir=parent_dir,
            executable_parallel=executable_parallel,
            properties=properties,
            defs=defs,
            required=required,
            type=type,
        )
        self.tasks[name] = task_dict

    def get_executable_path(
        self,
        name
    ):
        parent_dir = self.tasks[name]['parent_dir']
        exec_fname = self.tasks[name]['executable_parallel']

        return os.path.join(parent_dir, exec_fname)

    def get_package_name(
        self,
        name
    ):

        return self.tasks[name]['package_name']

    def get_run_dir(self, name):
        """tasks/<package>/<version>/<task>/: the task's run files."""
        task = self.tasks[name]
        pkg, _, pkg_version = task['package_name'].partition('==')
        version = task.get('version') or pkg_version or 'unversioned'
        return catalog.task_run_dir(pkg, version, task['base_name'])

    def get_path_to_json(self, name):
        """Arguments JSON passed to the task (--args-json)."""
        return str(self.get_run_dir(name) / f"{self.tasks[name]['title']}.json")

    def get_out_json_path(self, name):
        """Output JSON the task writes (--out-json)."""
        return str(self.get_run_dir(name) / 'out_file.json')

    def get_task(
        self,
        name
    ):
        return self.tasks[name]

    def get_properties(
        self,
        name
    ):
        return self.tasks[name]['properties']


class TasksQWidget(QWidget):
    def __init__(self, napari_viewer):
        super().__init__()
        self._viewer = napari_viewer
        self.exec_btn_dict = dict()
        self._task_ui = dict()       # task key -> widgets of its tab
        self._task_workers = []      # running task processes
        self.task_manager = FractalTaskManager()

        # Installed packages: "<name>==<version>" -> catalog install entry
        self.loaded_packages = dict()

        # ---------------- Main container ----------------
        ### Core widget components
        self.main_container = QWidget()
        main_layout = QVBoxLayout()
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(15, 15, 15, 15)
        self.main_container.setLayout(main_layout)
        self.main_container.setMinimumWidth(400)

        # ---------------- Title & Logo ----------------
        main_title = QLabel("Fractal Task Launcher")
        main_title.setFont(QFont('Arial', 16, weight=QFont.Bold))
        icon_img_container = QWidget()
        icon_img_container.setLayout(QHBoxLayout())
        im_path = abspath(__file__, 'logo_images/fractal_logo.png')
        icon_img = QPixmap(im_path).scaled(120, 120, Qt.KeepAspectRatio,
                                           Qt.SmoothTransformation)
        icon_label = QLabel()
        icon_label.setPixmap(icon_img)
        icon_label.setFixedSize(130, 130)
        icon_img_container.layout().addWidget(icon_label)

        # ---------------- Image input ----------------
        image_input_container = QWidget()
        image_input_container.setLayout(QHBoxLayout())
        image_input_label = QLabel('Input:')
        image_input_label.setFont(QFont('Arial', 14, weight=QFont.Bold))
        image_input_container.layout().addWidget(image_input_label)
        self._image_layers = QComboBox(self)
        image_input_container.layout().addWidget(self._image_layers)
        image_input_container.layout().setSpacing(0)

        # ---------------- Crop to ROI section ----------------
        crop_container = QGroupBox("(Optional) Custom ROI selection")
        crop_container.setMinimumHeight(150)
        crop_layout = QVBoxLayout()
        crop_layout.setSpacing(10)
        crop_layout.setContentsMargins(15, 15, 15, 15)
        crop_container.setLayout(crop_layout)

        self.add_shapes_button = QPushButton("Add Shapes Layer for ROI selection")
        self.add_shapes_button.clicked.connect(self._add_shapes_layer)
        crop_layout.addWidget(self.add_shapes_button)

        # ROI Table Name: label + input in horizontal layout
        roi_table_container = QWidget()
        roi_table_container.setLayout(QHBoxLayout())
        roi_table_label = QLabel("ROI Table Name:")
        roi_table_label.setFont(QFont('Arial', 14, weight=QFont.Bold))
        self.roi_table_input = QLineEdit("Cropped_FOV_ROI_table")
        roi_table_container.layout().addWidget(roi_table_label)
        roi_table_container.layout().addWidget(self.roi_table_input)
        roi_table_container.layout().setSpacing(5)
        crop_layout.addWidget(roi_table_container)

        self.roi_overwrite_checkbox = QCheckBox("Overwrite")
        crop_layout.addWidget(self.roi_overwrite_checkbox)

        self.crop_button_init = QPushButton("Save ROIs to table")
        self.crop_button_init.clicked.connect(self._handle_crop_button_clicked)
        crop_layout.addWidget(self.crop_button_init)

        # ---------------- Fractal task catalog ----------------
        self.catalog_panel = CatalogPanel(self)
        self.catalog_panel.installed.connect(self._on_catalog_installed)

        # ---------------- Load task: pick an installed task, open its tab ----
        load_task_container = QGroupBox("Load task")
        load_task_layout = QVBoxLayout()
        load_task_layout.setSpacing(10)
        load_task_layout.setContentsMargins(15, 15, 15, 15)
        load_task_container.setLayout(load_task_layout)

        select_task_container = QWidget()
        select_task_container.setLayout(QHBoxLayout())
        select_task_label = QLabel("Select task:")
        select_task_label.setFont(QFont('Arial', 14, weight=QFont.Bold))
        select_task_container.layout().addWidget(select_task_label)
        self.workflow_combo_box = QComboBox(self)
        select_task_container.layout().addWidget(self.workflow_combo_box)
        load_task_layout.addWidget(select_task_container)

        self.task_adder_btn = QPushButton("Add task")
        self.task_adder_btn.clicked.connect(self._add_task)
        load_task_layout.addWidget(self.task_adder_btn)

        # ---------------- Assemble main container ----------------
        main_layout.addWidget(main_title)
        main_layout.addWidget(icon_img_container)
        main_layout.addWidget(image_input_container)
        main_layout.addWidget(crop_container)
        main_layout.addWidget(self.catalog_panel)
        main_layout.addWidget(load_task_container)
        main_layout.addStretch(1)  # pushes everything up nicely


        # ---------------- Tab container ----------------
        self.tab_container = QTabWidget()
        main_scroll = QScrollArea()
        main_scroll.setWidgetResizable(True)
        main_scroll.setWidget(self.main_container)
        self.tab_container.addTab(main_scroll, "Main")

        self.setLayout(QHBoxLayout())
        self.layout().addWidget(self.tab_container)

        self._update_combo_boxes()
        # Keep the input dropdown in sync with the viewer (e.g. task results)
        self._viewer.layers.events.inserted.connect(lambda _e: self._update_combo_boxes())
        self._viewer.layers.events.removed.connect(lambda _e: self._update_combo_boxes())

    def _update_combo_boxes(self):
        for layer_name in [self._image_layers.itemText(i) for i in range(self._image_layers.count())]:
            layer_name_index = self._image_layers.findText(layer_name)
            self._image_layers.removeItem(layer_name_index)

        for layer in [l for l in self._viewer.layers if isinstance(l, napari.layers.Image)]:
            if layer.name not in [self._image_layers.itemText(i) for i in range(self._image_layers.count())]:
                self._image_layers.addItem(layer.name)

    def _on_catalog_installed(self, pkg, version, info, entry, selected_task):
        """Register an installed (package, version) and open the chosen task."""
        package_key = f"{pkg.pkg_name}=={version}"
        self.loaded_packages[package_key] = entry

        # Prefer the manifest of the installed package; fall back to the wheel's
        try:
            manifest = self._get_json_params(entry['manifest_path'])
        except OSError:
            manifest = info.manifest

        selected_key = None
        for task in manifest["task_list"]:
            if task_type(task) != "parallel":
                continue
            key = f"{task['name']} [{package_key}]"
            if key not in self.task_manager.tasks:
                schema = task["args_schema_parallel"]
                self.task_manager.add_task(
                    name=key,
                    base_name=task["name"],
                    version=version,
                    package_name=package_key,
                    parent_dir=entry["src_path"],
                    executable_parallel=task["executable_parallel"],
                    properties=schema["properties"],
                    defs=schema.get("$defs", None),
                    required=schema.get("required", []),
                    type=schema.get("type"),
                    title=schema.get("title", task["name"]),
                    docs_info=task.get("docs_info"),
                    docs_link=task.get("docs_link"),
                )
                self.workflow_combo_box.addItem(key)
            if task["name"] == selected_task:
                selected_key = key

        if selected_key is not None:
            self.workflow_combo_box.setCurrentText(selected_key)
            self._add_task()

    def _fetch_subprocess_output(self, task_name):
        """Show what a task wrote: the outputs named by its arguments (label
        images in the input image, new images next to it), the images it
        reports in its --out-json file, and any other new label images."""
        wipe_cache()
        ui = self._task_ui[task_name]
        args = ui['args']
        path_to_zarr = args['zarr_url']
        props = self.task_manager.get_properties(task_name)
        out_json = self.task_manager.get_out_json_path(task_name)

        labels_on_disk = set(_list_labels(path_to_zarr))
        expected_labels = expected_label_names(args, props)
        label_names = (labels_on_disk & set(expected_labels)) | (
            labels_on_disk - ui.get('labels_before', set())
        )
        missing = [f"label '{n}' in {path_to_zarr}"
                   for n in expected_labels if n not in labels_on_disk]

        image_urls = reported_image_urls(out_json, path_to_zarr)
        for url in expected_image_urls(args, props, path_to_zarr):
            if os.path.isdir(url):
                if url not in image_urls:
                    image_urls.append(url)
            elif url not in image_urls:
                missing.append(f"image {url}")
        logger.info(
            f'{task_name}: labels {sorted(label_names)}, images {image_urls}, '
            f'missing {missing}'
        )

        shown = []
        not_loaded = []
        if label_names:
            added = self._load_zarr_layers(path_to_zarr, labels=label_names)
            shown += added
            not_loaded += [f"label '{n}' from {path_to_zarr}"
                           for n in sorted(label_names) if n not in added]
        for url in image_urls:
            added = self._load_zarr_layers(url, suffix=os.path.basename(url))
            shown += added
            if not added:
                not_loaded.append(f"image {url}")

        problems = []
        if missing:
            problems.append(f"did not write the expected output: {'; '.join(missing)}")
        if not_loaded:
            problems.append(f"could not load {'; '.join(not_loaded)}")
        if shown:
            show_info(f"{task_name}: added {', '.join(shown)}")
        if problems:
            show_warning(f"{task_name} finished, but {' and '.join(problems)}")
        elif not shown:
            show_info(f"{task_name} finished (it has no label or image output to display).")

        self._update_execute_buttons(is_enabled=True)

    def _load_zarr_layers(self, zarr_url, labels=None, suffix=None):
        """
        Add layers of an OME-Zarr image to the viewer.

        labels: only add these label layers (and no image layers).
        suffix: add everything, naming layers "<name> [<suffix>]".
        """
        reader = napari_get_reader(zarr_url)
        if reader is None:
            logger.warning(f'Cannot read {zarr_url}')
            return []
        added = []
        for layer_data in reader(zarr_url):
            data = layer_data[0]
            meta = dict(layer_data[1]) if len(layer_data) > 1 else {}
            layer_type = layer_data[2] if len(layer_data) > 2 else 'image'
            if layer_type == 'labels':
                meta['name'] = _label_name(meta.get('name'))
            if labels is not None:
                if layer_type != 'labels' or meta.get('name') not in labels:
                    continue

            # Names may be a list (one per channel)
            names = meta.get('name')
            if suffix and names is not None:
                if isinstance(names, (list, tuple)):
                    names = [f'{n} [{suffix}]' for n in names]
                else:
                    names = f'{names} [{suffix}]'
                meta['name'] = names

            # Replace stale layers with the same name(s); label layers may
            # have been opened under their path name
            for n in names if isinstance(names, (list, tuple)) else [names]:
                if n is None:
                    continue
                for layer in list(self._viewer.layers):
                    if layer.name == n or (
                        layer_type == 'labels'
                        and isinstance(layer, napari.layers.Labels)
                        and _label_name(layer.name) == n
                    ):
                        self._viewer.layers.remove(layer)

            new_layers = getattr(self._viewer, f'add_{layer_type}')(data, **meta)
            if not isinstance(new_layers, list):
                new_layers = [new_layers]
            for layer in new_layers:
                layer.visible = True
                # Lets the layer be used as input for the next task
                layer.metadata['zarr_url'] = zarr_url
                added.append(layer.name)
        return added

    def _update_execute_buttons(self, is_enabled=True):
        for name in self.exec_btn_dict.keys():
            self.exec_btn_dict[name].setEnabled(is_enabled)

    # ------------------------------------------------------------------
    # Running a task (in the background, with a live log)
    # ------------------------------------------------------------------

    def _selected_zarr_url(self):
        name = self._image_layers.currentText()
        if not name or name not in self._viewer.layers:
            return None
        layer = self._viewer.layers[name]
        return layer.source.path or layer.metadata.get('zarr_url')

    def _execute_task(self, task_name):
        ui = self._task_ui[task_name]
        path_to_zarr = self._selected_zarr_url()
        if not path_to_zarr:
            show_error("Select an OME-Zarr image layer as input (Main tab).")
            return

        try:
            args = ui['form'].get_args()
        except SchemaError as e:
            ui['status'].setText(f"⚠ {e}")
            show_error(f"{self.task_manager.get_task(task_name)['base_name']}: {e}")
            return
        args['zarr_url'] = path_to_zarr

        # Kept to find the task's outputs after the run
        ui['args'] = args

        self.task_manager.get_run_dir(task_name).mkdir(parents=True, exist_ok=True)
        path_to_task_args = self.task_manager.get_path_to_json(task_name)
        with open(path_to_task_args, 'w') as f:
            json.dump(args, f, indent=1)
        out_json = self.task_manager.get_out_json_path(task_name)
        if os.path.exists(out_json):
            os.remove(out_json)

        # Remember existing labels so new ones can be shown after the run
        ui['labels_before'] = set(_list_labels(path_to_zarr))

        package_name = self.task_manager.get_package_name(task_name)
        manifest_path = self.loaded_packages[package_name]['pixi_manifest_path']
        try:
            pixi = find_pixi()
        except FileNotFoundError as e:
            show_error(str(e))
            return
        entry = self.loaded_packages[package_name]
        frozen = ['--frozen'] if entry.get('pixi_frozen') else []
        cmd = [
            pixi, 'run', *frozen, '--manifest-path', manifest_path,
            'python', self.task_manager.get_executable_path(task_name),
            '--args-json', path_to_task_args, '--out-json', out_json,
        ]

        self._update_execute_buttons(is_enabled=False)
        ui['log'].clear()
        ui['log'].appendPlainText(f"$ {' '.join(cmd)}\n")
        ui['progress'].setVisible(True)
        ui['status'].setText(f"Running on {os.path.basename(path_to_zarr)}…")
        ui['t0'] = time.monotonic()

        worker = create_worker(_stream_process, cmd)
        worker.yielded.connect(ui['log'].appendPlainText)
        worker.returned.connect(lambda rc: self._on_task_finished(task_name, rc))
        worker.errored.connect(lambda e: self._on_task_finished(task_name, -1, e))
        self._task_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._task_workers.remove(w))
        worker.start()

    def _on_task_finished(self, task_name, returncode, error=None):
        ui = self._task_ui.get(task_name)
        self._update_execute_buttons(is_enabled=True)
        if ui is None:  # tab closed while running
            return
        ui['progress'].setVisible(False)
        elapsed = time.monotonic() - ui.get('t0', time.monotonic())
        if returncode == 0:
            ui['status'].setText(f"✓ Finished in {elapsed:.0f} s")
            self._fetch_subprocess_output(task_name)
        else:
            lines = ui['log'].toPlainText().strip().splitlines()
            last = str(error) if error else (lines[-1] if lines else "")
            ui['status'].setText(f"✗ Failed after {elapsed:.0f} s: {last[:200]}")
            ui['log_section'].button.setChecked(True)
            show_error(f"{self.task_manager.get_task(task_name)['base_name']} failed: {last[:300]}")

    # ------------------------------------------------------------------
    # Task tabs
    # ------------------------------------------------------------------

    def _task_tab_exists(self, task_name):
        return task_name in self._task_ui

    def _add_task(self):
        task_name = self.workflow_combo_box.currentText()
        if not task_name:
            return
        if self._task_tab_exists(task_name):
            self.tab_container.setCurrentWidget(self._task_ui[task_name]['page'])
        else:
            self._add_task_tab(task_name)

    def _channel_suggestions(self):
        """Channel labels of the selected input image (for completers)."""
        url = self._selected_zarr_url()
        labels = _channel_labels(url) if url else []
        current = self._image_layers.currentText()
        if current and current not in labels:
            labels.append(current)
        return labels

    def _add_task_tab(self, task_name):
        task = self.task_manager.get_task(task_name)
        schema = {
            "type": "object",
            "properties": task["properties"],
            "required": task.get("required") or [],
            "$defs": task.get("defs") or {},
        }

        page = QWidget(objectName=task_name)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 10, 10, 10)

        # Header: name, package/version, short docs
        title = QLabel(task['base_name'])
        title.setFont(QFont('Arial', 14, weight=QFont.Bold))
        title.setWordWrap(True)
        outer.addWidget(title)
        subtitle = QLabel(task['package_name'])
        subtitle.setStyleSheet("color: gray;")
        outer.addWidget(subtitle)
        docs = (task.get('docs_info') or '').strip()
        if docs:
            paragraphs = [p.strip() for p in docs.split('\n\n')]
            lines = [ln for ln in '\n'.join(paragraphs).splitlines()
                     if ln.strip() and not ln.lstrip().startswith('#')]
            first = ' '.join(lines[:3]).strip()
            docs_label = QLabel(first[:300] + ('…' if len(first) > 300 else ''))
            docs_label.setWordWrap(True)
            docs_label.setToolTip(docs[:3000])
            outer.addWidget(docs_label)
        if task.get('docs_link'):
            link = QLabel(f'<a href="{task["docs_link"]}">Documentation</a>')
            link.setOpenExternalLinks(True)
            outer.addWidget(link)

        input_label = QLabel()
        input_label.setStyleSheet("color: gray;")

        def _update_input(*_):
            name = self._image_layers.currentText()
            input_label.setText(f"Input: {name}" if name else "Input: (select an image in the Main tab)")
        _update_input()
        self._image_layers.currentTextChanged.connect(_update_input)
        outer.addWidget(input_label)

        # Parameters
        channels = self._channel_suggestions()
        suggestions = {k: channels for k in CHANNEL_FIELDS}
        form = SchemaForm(schema, suggestions=suggestions)
        autofill = ChannelAutofill(form.fields)

        def _update_channel(*_):
            url = self._selected_zarr_url()
            labels = _channel_labels(url) if url else []
            autofill.set_channel(
                _input_channel(self._image_layers.currentText(), labels))
        _update_channel()
        self._image_layers.currentTextChanged.connect(_update_channel)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(form)
        outer.addWidget(scroll, 1)

        # Buttons
        buttons = QWidget()
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        run_btn = QPushButton("▶ Run")
        run_btn.setDefault(True)
        run_btn.setStyleSheet("font-weight: bold;")
        run_btn.clicked.connect(lambda: self._execute_task(task_name))
        reset_btn = QPushButton("Reset")
        reset_btn.setToolTip("Reset all parameters to the task defaults")
        reset_btn.clicked.connect(form.reset)
        close_btn = QPushButton("Remove task")
        close_btn.clicked.connect(lambda: self._close_tab(task_name))
        buttons_layout.addWidget(run_btn, 2)
        buttons_layout.addWidget(reset_btn, 1)
        buttons_layout.addWidget(close_btn, 1)
        outer.addWidget(buttons)

        progress = QProgressBar()
        progress.setRange(0, 0)  # busy indicator
        progress.setTextVisible(False)
        progress.setMaximumHeight(6)
        progress.setVisible(False)
        outer.addWidget(progress)

        status = QLabel("")
        status.setWordWrap(True)
        outer.addWidget(status)

        log = QPlainTextEdit()
        log.setReadOnly(True)
        log.setMaximumBlockCount(5000)
        log.setFont(QFont('Menlo, Courier New', 9))
        log.setMinimumHeight(160)
        log_section = _Collapsible("Log", log, expanded=False)
        outer.addWidget(log_section)

        self.exec_btn_dict[task_name] = run_btn
        self._task_ui[task_name] = dict(
            page=page, form=form, run_btn=run_btn, progress=progress,
            status=status, log=log, log_section=log_section,
            input_slots=(_update_input, _update_channel),
        )

        short = task['base_name'] if len(task['base_name']) <= 22 else task['base_name'][:21] + '…'
        idx = self.tab_container.addTab(page, short)
        self.tab_container.setTabToolTip(idx, task_name)
        self.tab_container.setCurrentIndex(idx)

    def _close_tab(self, task_name):
        ui = self._task_ui.pop(task_name, None)
        self.exec_btn_dict.pop(task_name, None)
        if ui is not None:
            for slot in ui['input_slots']:
                self._image_layers.currentTextChanged.disconnect(slot)
            idx = self.tab_container.indexOf(ui['page'])
            if idx >= 0:
                self.tab_container.removeTab(idx)
            ui['page'].deleteLater()

    def _get_json_params(self, path_to_json):
        with open(path_to_json) as f:
            return json.load(f)

    def _add_shapes_layer(self):
        """Add Shapes layer to napari viewer for ROI selection."""
        shapes_layer = Shapes(name="ROI selection", shape_type='polygon')
        self._viewer.add_layer(shapes_layer)

    def _handle_crop_button_clicked(self):
        table_name = self.roi_table_input.text().strip()
        overwrite = self.roi_overwrite_checkbox.isChecked()

        if table_name == "":
            QMessageBox.warning(
                None,
                "Missing table name",
                "Please enter an ROI table name before continuing."
            )
            return

        # Pass both values to the crop function
        self._crop_image_to_rois(table_name, overwrite)


    def _crop_image_to_rois(self, table_name: str, overwrite: bool):
        """Crop selected image to ROI defined by Shapes layer."""
        image_name = self._image_layers.currentText()
        if not image_name:
            logger.warning("No image layer selected.")
            return

        # Get selected image layer from the napari viewer
        image_layer = self._viewer.layers[image_name]

        # Find all shape layers
        shapes_layers = [layer for layer in self._viewer.layers
                         if isinstance(layer, Shapes)]

        if not shapes_layers:
            logger.warning("Please first select a ROI with a Polygon Shape Layer.")
            return

        shapes_layer = shapes_layers[0]
        if not shapes_layer.data:
            logger.warning("Shapes layer has no data.")
            return

        # TODO: add support for multiple shape layers
        if len(shapes_layers) > 1:
            logger.warning("Multiple Shapes layers detected. Using the most recent one.")
        shapes_layer = shapes_layers[0]

        # Create ROIs from shape layer polygons
        cropped_rois = []
        for roi_id, polygon_array in enumerate(shapes_layer.data):
            polygon_array = np.array(polygon_array)
            crop_roi = create_roi_from_bbox(polygon_array, roi_id=roi_id+1)
            cropped_rois.append(crop_roi)

        # Save to table
        ome_zarr = open_ome_zarr_container(image_layer.source.path)
        roi_table_crops = RoiTable(rois=cropped_rois)
        ome_zarr.add_table(table_name, roi_table_crops, overwrite=overwrite)

        # Just for testing
        # imgs = ome_zarr.get_image("0")
        # pixel_sizes = imgs.pixel_size.zyx
        # for roi in roi_table_crops.rois():
        #     cropped_img = imgs.get_roi_as_numpy(roi, c=0)
        #     # add to viewer
        #     self._viewer.add_image(cropped_img, name=f"Cropped_FOV_ROI_{roi.name}",
        #                            scale=pixel_sizes, blending="additive")

        logger.info(f"Finished cropping {image_name} to ROI(s).")
