import logging
from typing import TYPE_CHECKING

from qtpy.QtWidgets import (QHBoxLayout, QPushButton, QWidget, QTabWidget,
                            QVBoxLayout, QLabel,
                            QLineEdit, QFileDialog, QCheckBox, QComboBox,
                            QGroupBox, QScrollArea, QPlainTextEdit,
                            QProgressBar)
from qtpy.QtGui import QPixmap, QFont
from qtpy.QtCore import Qt

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QLineEdit, QCheckBox, QMessageBox

import ast
import json
import subprocess
import os
import time

import napari
from napari.layers import Shapes
from napari.qt.threading import create_worker
from napari.utils.notifications import show_error, show_info
from napari_ome_zarr._reader import napari_get_reader

import dask.array as da
import numpy as np

from ngio import open_ome_zarr_container
from ngio.tables import RoiTable

from napari_workflow_tasks._utils import create_roi_from_bbox, NapariHandler
from pathlib import Path

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
from .io_utils import PackageImporter

from .pixi_utils import pixi_runner
from ._catalog_panel import CatalogPanel
from .catalog import find_pixi, task_type
from ._schema_form import ListField, SchemaError, SchemaForm, _Collapsible

if TYPE_CHECKING:
    import napari

# from io_utils import PackageImporter

# TODO: Automatically decide what properties to ignore based on MANIFEST
IGNORE_PROPERTIES = ['zarr_url', 'channels_to_include', 'channels_to_exclude', 'measure_texture'] #, 'channel'
INCLUDE_CATEGORIES = ["Segmentation", "Measurement"]

# ---------------------------------------------------------------------------
# Converting text-box input into JSON values matching the task's JSON Schema
# ---------------------------------------------------------------------------

# Sentinel for "no value given": the argument is omitted from the args JSON,
# so the task (pydantic) applies its own default.
MISSING = object()


def _is_ref_value(value):
    return isinstance(value, dict) and set(value) == {'args', 'type'}


def _schema_types(schema):
    """All JSON types a property accepts (handles anyOf/oneOf/$ref)."""
    t = schema.get('type')
    types = set(t) if isinstance(t, list) else ({t} if t else set())
    for sub in schema.get('anyOf', []) + schema.get('oneOf', []):
        if 'type' in sub:
            types.add(sub['type'])
        if '$ref' in sub:
            types.add('object')
    if '$ref' in schema or 'oneOf' in schema or 'allOf' in schema:
        types.add('object')
    return types


def _parse_literal(text):
    """Parse JSON (or Python-literal) text; return MISSING if impossible."""
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(text)
        except (ValueError, SyntaxError, TypeError):
            pass
    return MISSING


def _convert_scalar(text, types):
    if 'integer' in types:
        try:
            return int(text)
        except ValueError:
            pass
    if 'number' in types or 'float' in types:
        try:
            return float(text)
        except ValueError:
            pass
    if 'boolean' in types and text.lower() in ('true', 'false'):
        return text.lower() == 'true'
    return text


def parse_text_value(text, schema):
    """Turn the text of a QLineEdit into a value valid for ``schema``."""
    text = text.strip()
    if text in ('', 'None', 'null'):
        return MISSING

    types = _schema_types(schema)

    if 'array' in types:
        if text.startswith('['):
            value = _parse_literal(text)
            if value is not MISSING:
                return value
        # Comma-separated list, e.g. "DAPI, nanog"
        item_types = _schema_types(schema.get('items', {})) or {'string'}
        return [
            _convert_scalar(x.strip(), item_types)
            for x in text.split(',') if x.strip()
        ]

    if 'object' in types or not types:
        if text[0] in '{[' or not types:
            value = _parse_literal(text)
            if value is not MISSING:
                return value
        if 'string' not in types and types:
            return MISSING

    return _convert_scalar(text, types)


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


def _prefill_channel(fields, layer_name, channels):
    """Fill empty channel-identifier lists that need a value (required, or
    with ``minItems``) with the input layer's channel (napari-ome-zarr names
    image layers after their channel). Optional nested models are left
    untouched: prefilling would make the form send them."""
    if not layer_name or layer_name not in channels:
        return
    for key, field in fields.items():
        inner = getattr(field, 'form', None)
        if inner is not None:
            if not getattr(field, 'optional', False):
                _prefill_channel(inner.fields, layer_name, channels)
        elif (
            key == 'identifiers' and isinstance(field, ListField)
            and (field.required or field.schema.get('minItems'))
        ):
            if not field.edit.text().strip():
                field.set_value([layer_name])


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

    def setup_logging(self):
        for handler in logger.root.handlers[:]:
            logging.root.removeHandler(handler)
        # Create a custom handler for napari
        napari_handler = NapariHandler()
        napari_handler.setLevel(logging.INFO)

        logger.addHandler(napari_handler)

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
            widget_dict=dict()
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

    def get_parent_dir(
        self,
        name
    ):

        return self.tasks[name]['parent_dir']

    def get_path_to_json(
        self,
        name
    ):

        parent_dir = self.tasks[name]['parent_dir']
        title = self.tasks[name]['title']
        return os.path.join(parent_dir, f'{title}.json')

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

    def get_defs(
        self,
        name
    ):
        return self.tasks[name]['defs']

    def write_to_json(
        self,
        name
    ):
        # Write json to ".napari_workflow_tasks" directory inside "params" directory
        parent_dir = self.tasks[name]['parent_dir']
        title = self.tasks[name]['title']
        path_to_json = os.path.join(parent_dir, f'{title}.json')

        args_dict = dict()
        for prop_key, schema in self.tasks[name]['properties'].items():
            if 'value' not in schema:
                continue
            value = schema['value']
            if _is_ref_value(value):
                # Nested model: {'args': {...}, 'type': <model title>}
                value = value['args']
            if value is MISSING:
                # Left empty / "None": let the task use its own default
                continue
            if 'default' in schema and value == schema['default']:
                # Unchanged default: omit, so the task's own default applies
                continue
            args_dict[prop_key] = value

        with open(path_to_json, 'w') as f:
            json.dump(args_dict, f)

    def get_title(
        self,
        name
    ):

        return self.tasks[name]['title']

    def update_task_property(
        self,
        name,
        property,
        value
    ):

        print('Property dict updated', name, property, value)
        try:
            self.tasks[name]['properties'][property]['value'] = value
        except KeyError:
            print(f'Property {property} not defined in MANIFEST')

    def add_widget_dict(
        self,
        name,
        widget_dict
    ):

        self.tasks[name]['widget_dict'] = widget_dict

    def remove_widget_dict(
        self,
        name
    ):

        self.tasks[name]['widge_dict'] = dict()

    def get_widget_value(
        self,
        name,
        property
    ):

        widget = self.tasks[name]['widget_dict'][property]
        schema = self.tasks[name]['properties'][property]

        if isinstance(widget, QLineEdit):
            return parse_text_value(widget.text(), schema)

        elif isinstance(widget, QCheckBox):
            return widget.isChecked()

        elif isinstance(widget, dict):
            args_dict = dict()
            ref = os.path.split(schema['$ref'])[-1]
            defs = self.get_defs(name)
            defs_props = defs[ref]['properties']

            for key in widget.keys():
                if isinstance(widget[key], QLineEdit):
                    value = parse_text_value(widget[key].text(), defs_props.get(key, {}))
                    if value is not MISSING:
                        args_dict[key] = value

                elif isinstance(widget[key], QCheckBox):
                    args_dict[key] = widget[key].isChecked()

            return_dict = dict(args=args_dict,
                               type=defs[ref]['title'])

            return return_dict


class TaskWorker(QObject):
    finished = pyqtSignal(str)
    progress = pyqtSignal(int)

    @property
    def manifest_path(self):
        return self._manifest_path

    @manifest_path.setter
    def manifest_path(self, path):
        # Add checks for validity
        print(f'Set manifest_path as {path}')
        self._manifest_path = path

    @property
    def task_name(self):
        return self._task_name

    @task_name.setter
    def task_name(self, name):
        # Add checks for validity
        logger.info(f'Set task_name as {name}')
        self._task_name = name

    @property
    def task_manager(self):
        return self._task_manager

    @task_manager.setter
    def task_manager(self, task_manager):
        # Add checks for validity
        logger.info(f'Set task_manager')
        self._task_manager = task_manager

    @pyqtSlot()
    def run(self):
        logger.info('Thread running')
        task_name = self._launch_task_subprocess(self.task_name)
        self.finished.emit(task_name)

    def _launch_task_subprocess(self, task_name):
        logger.info('Launching subprocess...')
        path_to_executable = self.task_manager.get_executable_path(task_name)

        path_to_task_args = self.task_manager.get_path_to_json(task_name)

        p = subprocess.Popen(['pixi', 'run', '--manifest-path', self.manifest_path, os.path.join(os.path.dirname(__file__), 'task_wrapper.py'), '--executable', path_to_executable, '--path_to_task_args', path_to_task_args]) #Pass wrapper_args: path to executable
        p.wait()

        logger.info('Finished running subprocess')

        return task_name


class TasksQWidget(QWidget):
    def __init__(self, napari_viewer):
        super().__init__()
        self._viewer = napari_viewer
        self.exec_btn_dict = dict()
        self._task_ui = dict()       # task key -> widgets of its tab
        self._task_workers = []      # running task processes
        self.task_manager = FractalTaskManager()

        # ---------------- Main container ----------------
        ### Add PackageImporter
        self.package_importer = PackageImporter()

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

        # ---------------- Workflow / Add Task Package ----------------
        workflow_container = QGroupBox("Workflow / Add Task Package")
        workflow_container.setMinimumHeight(150)
        workflow_layout = QVBoxLayout()
        workflow_layout.setSpacing(10)
        workflow_layout.setContentsMargins(15, 15, 15, 15)
        workflow_container.setLayout(workflow_layout)

        self.workflow_adder_btn = QPushButton("Add task package")
        self.workflow_adder_btn.clicked.connect(self._select_workflow_file)
        workflow_layout.addWidget(self.workflow_adder_btn)

        select_workflow_container = QWidget()
        select_workflow_container.setLayout(QHBoxLayout())
        workflow_label = QLabel("Select task:")
        workflow_label.setFont(QFont('Arial', 14, weight=QFont.Bold))
        select_workflow_container.layout().addWidget(workflow_label)
        self.workflow_combo_box = QComboBox(self)
        select_workflow_container.layout().addWidget(self.workflow_combo_box)
        workflow_layout.addWidget(select_workflow_container)

        # ---------------- Fractal task catalog ----------------
        self.catalog_panel = CatalogPanel(self)
        self.catalog_panel.installed.connect(self._on_catalog_installed)

        # ---------------- Task Adder Section ----------------
        task_adder_container = QGroupBox("Add Task")
        task_adder_layout = QHBoxLayout()
        task_adder_layout.setContentsMargins(15, 15, 15, 15)
        task_adder_container.setLayout(task_adder_layout)

        self.task_adder_btn = QPushButton("Add task")
        self.task_adder_btn.clicked.connect(self._add_task)
        task_adder_layout.addWidget(self.task_adder_btn)

        # ---------------- Assemble main container ----------------
        main_layout.addWidget(main_title)
        main_layout.addWidget(icon_img_container)
        main_layout.addWidget(image_input_container)
        main_layout.addWidget(crop_container)
        main_layout.addWidget(self.catalog_panel)
        main_layout.addWidget(workflow_container)
        main_layout.addWidget(task_adder_container)
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

    def _select_workflow_file(self):
        # Open workflow package with PackageImporter

        path_to_package = QFileDialog().getOpenFileName(self, "Select workflow package", ".",
                                                         "workflow specs (*.tar.gz)")[0]

        self.package_importer.open_package(path_to_package)

        path_to_workflow = self.package_importer.loaded_packages[self.package_importer.package_name]['manifest_path']

        print(os.path.split(path_to_workflow)[0])

        workflow_args = self._get_json_params(path_to_workflow)

        for task in workflow_args["task_list"]:
            # if task.get("category") in INCLUDE_CATEGORIES:
            is_parallel = True if task['type'] == "parallel" else False

            if is_parallel:
                self.workflow_combo_box.addItem(task["name"])
                self.task_manager.add_task(name=task["name"],
                                           package_name=self.package_importer.package_name,
                                           parent_dir=os.path.split(path_to_workflow)[0],
                                           executable_parallel=task["executable_parallel"],
                                           properties=task["args_schema_parallel"]["properties"],
                                           defs=task["args_schema_parallel"].get("$defs", None),
                                           required=task["args_schema_parallel"]["required"],
                                           type=task["args_schema_parallel"]["type"],
                                           title=task["args_schema_parallel"]["title"])

    def _on_catalog_installed(self, pkg, version, info, entry, selected_task):
        """Register an installed (package, version) and open the chosen task."""
        package_key = f"{pkg.pkg_name}=={version}"
        self.package_importer.loaded_packages[package_key] = entry

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
        """Show what a task produced: new/updated labels and new images."""
        logger.info(f'Received task_name={task_name}')
        wipe_cache()
        props = self.task_manager.get_properties(task_name)
        path_to_zarr = props['zarr_url']['value']

        # 1. Labels written into the input image: new ones, plus the
        #    task's output-label argument (covers overwriting an existing one)
        labels_after = set(_list_labels(path_to_zarr))
        label_names = labels_after - getattr(self, '_labels_before', set())
        out_label = self._output_label_name(task_name)
        if out_label in labels_after:
            label_names.add(out_label)

        # 2. New images reported by the task (e.g. "0_gaussian_blur"), from
        #    the "image_list_updates" in the task's --out-json file
        out_json = os.path.join(
            os.path.dirname(self.task_manager.get_path_to_json(task_name)),
            "out_file.json",
        )
        new_zarr_urls = []
        if os.path.exists(out_json):
            try:
                with open(out_json) as f:
                    metadata = json.load(f) or {}
            except (OSError, ValueError):
                metadata = {}
            for update in metadata.get('image_list_updates', []):
                url = update.get('zarr_url')
                if url and os.path.normpath(url) != os.path.normpath(path_to_zarr):
                    new_zarr_urls.append(url)

        shown = []
        if label_names:
            shown += self._load_zarr_layers(path_to_zarr, labels=label_names)
        for url in new_zarr_urls:
            shown += self._load_zarr_layers(url, suffix=os.path.basename(url))

        if shown:
            show_info(f"{task_name}: added {', '.join(shown)}")
        else:
            show_info(f"{task_name} finished (nothing new to display).")

        self._update_execute_buttons(is_enabled=True)

    def _output_label_name(self, task_name):
        """Resolve the task's output label argument, if it has one."""
        props = self.task_manager.get_properties(task_name)
        for key in ('label_name', 'output_label_name'):
            if key not in props:
                continue
            name = props[key].get('value', MISSING)
            if name is MISSING or name is None:
                name = props[key].get('default')
            if isinstance(name, str) and '{channel_identifier}' in name:
                # e.g. "{channel_identifier}_segmented" -> "DAPI_segmented"
                channels = props.get('channels', {}).get('value')
                if _is_ref_value(channels):
                    channels = channels['args']
                identifiers = channels.get('identifiers') if isinstance(channels, dict) else None
                identifier = identifiers[0] if identifiers else self._image_layers.currentText()
                name = name.replace('{channel_identifier}', str(identifier))
            return name
        return None

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

            # Replace stale layers with the same name(s)
            for n in names if isinstance(names, (list, tuple)) else [names]:
                if n is not None and n in self._viewer.layers:
                    self._viewer.layers.remove(n)

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

        # Keep the values: used to find the outputs after the run
        for key, prop in self.task_manager.get_properties(task_name).items():
            prop['value'] = args.get(key, MISSING)

        path_to_task_args = self.task_manager.get_path_to_json(task_name)
        with open(path_to_task_args, 'w') as f:
            json.dump(args, f, indent=1)
        out_json = os.path.join(os.path.dirname(path_to_task_args), "out_file.json")
        if os.path.exists(out_json):
            os.remove(out_json)

        # Remember existing labels so new ones can be shown after the run
        self._labels_before = set(_list_labels(path_to_zarr))

        package_name = self.task_manager.get_package_name(task_name)
        manifest_path = self.package_importer.loaded_packages[package_name]['pixi_manifest_path']
        try:
            pixi = find_pixi()
        except FileNotFoundError as e:
            show_error(str(e))
            return
        entry = self.package_importer.loaded_packages[package_name]
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
        _prefill_channel(form.fields, self._image_layers.currentText(), channels)
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
        )
        self.task_manager.add_widget_dict(task_name, form.fields)

        short = task['base_name'] if len(task['base_name']) <= 22 else task['base_name'][:21] + '…'
        idx = self.tab_container.addTab(page, short)
        self.tab_container.setTabToolTip(idx, task_name)
        self.tab_container.setCurrentIndex(idx)

    def _close_tab(self, task_name):
        ui = self._task_ui.pop(task_name, None)
        self.exec_btn_dict.pop(task_name, None)
        self.task_manager.remove_widget_dict(task_name)
        if ui is not None:
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
