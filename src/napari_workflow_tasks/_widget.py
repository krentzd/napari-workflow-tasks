import logging
import os
import re
import time
from pathlib import Path

import napari
from napari.layers import Shapes
from napari.qt.threading import create_worker
from napari.utils.notifications import show_error, show_info, show_warning
from napari_ome_zarr._reader import napari_get_reader
from qtpy.QtCore import QPointF, QSize, Qt, Signal
from qtpy.QtGui import (QColor, QFont, QFontDatabase, QIcon, QPainter,
                         QPen, QPixmap, QPolygonF)
from qtpy.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QGroupBox,
                            QHBoxLayout, QLabel, QLineEdit, QListWidget,
                            QListWidgetItem, QMessageBox,
                            QPlainTextEdit, QProgressBar, QPushButton,
                            QScrollArea, QTabWidget, QVBoxLayout,
                            QWidget)

from workflow_tasks_core.args_io import (args_filename, export_args,
                                         export_workflow, read_args,
                                         write_json)

from workflow_tasks_core.outputs import (CHANNEL_PLACEHOLDER, LABEL_NAME_ARGS,
                                         find_task_outputs, new_image_urls,
                                         new_label_names, rewritten_inputs)
from workflow_tasks_core.pixi_config import find_pixi
from workflow_tasks_core.rois import write_roi_table
from workflow_tasks_core.runner import (TaskJob, default_zarr_dir,
                                        image_urls, pixi_run_prefix,
                                        run_task_job, short_name,
                                        stream_process, task_command)
from workflow_tasks_core.schema import SchemaError, with_defs
from workflow_tasks_core.tasks import (FractalTaskManager, docs_summary,
                                       load_manifest, register_manifest_tasks)
from workflow_tasks_core.zarr_meta import channel_labels, list_labels

from ._catalog_panel import CatalogPanel
from ._schema_form import ListField, SchemaForm, StringField, _Collapsible

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Fields that take channel names: offer the input image's channels
CHANNEL_FIELDS = ('identifiers', 'label', 'channel_label', 'channel_labels')


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


def layer_image_urls(layer) -> list[str]:
    """Images behind a napari layer (opened from disk or added by the plugin)."""
    path = getattr(getattr(layer, "source", None), "path", None)
    path = path or layer.metadata.get("zarr_url")
    return image_urls(path)


def _from_other_image(layer, zarr_url) -> bool:
    """Whether ``layer`` is known to show a different image than
    ``zarr_url`` (layers of unknown origin are not)."""
    urls = {os.path.normpath(u) for u in layer_image_urls(layer)}
    return bool(urls) and os.path.normpath(zarr_url) not in urls


def _without_napari_suffix(layer_name):
    """Layer name without the " [1]", " [2]", ... napari adds to a name
    that is already taken (e.g. "org [1]" -> "org")."""
    return re.sub(r' \[\d+\]$', '', layer_name)


def _label_name(layer_name):
    """Label image name of a label layer. napari-ome-zarr 0.10 names label
    layers by their path ("labels/labels/<name>"), 0.6 by the name only."""
    return layer_name.rsplit('/', 1)[-1] if isinstance(layer_name, str) else layer_name


class _InputImages(QWidget):
    """Check list of the OME-Zarr images loaded in the viewer."""

    changed = Signal()

    def __init__(self, get_images, default=None, parent=None):
        super().__init__(parent)
        self._get_images = get_images
        self._default = default  # url -> bool; None = check all
        self._state = {}         # remembered check states per url
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMaximumHeight(130)
        self.list.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        for text, value in (("All", True), ("None", False)):
            b = QPushButton(text)
            b.setMaximumWidth(60)
            b.clicked.connect(lambda _=False, v=value: self._check_all(v))
            buttons.addWidget(b)
        self.count = QLabel()
        self.count.setStyleSheet("color: gray;")
        buttons.addWidget(self.count, 1)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self, *_):
        images = self._get_images()
        self.list.blockSignals(True)
        self.list.clear()
        for url, layers in images.items():
            if url not in self._state:
                self._state[url] = self._default(url) if self._default else True
            item = QListWidgetItem(short_name(url))
            item.setData(Qt.UserRole, url)
            item.setToolTip(f"{url}\nLayers: {', '.join(layers)}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if self._state[url] else Qt.Unchecked)
            self.list.addItem(item)
        if not images:
            item = QListWidgetItem("(open an OME-Zarr image in napari)")
            item.setFlags(Qt.NoItemFlags)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._update_count()

    def _on_item_changed(self, item):
        url = item.data(Qt.UserRole)
        if url:
            self._state[url] = item.checkState() == Qt.Checked
        self._update_count()

    def _check_all(self, value):
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.UserRole):
                item.setCheckState(Qt.Checked if value else Qt.Unchecked)

    def _update_count(self):
        n = len(self.selected())
        self.count.setText(f"{n} image{'s' if n != 1 else ''} selected")
        self.changed.emit()

    def selected(self):
        return [
            self.list.item(i).data(Qt.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).data(Qt.UserRole)
            and self.list.item(i).checkState() == Qt.Checked
        ]


def wipe_cache():
    from napari.utils import resize_dask_cache
    cache = resize_dask_cache()
    cache_bytes = cache.cache.available_bytes
    cache = resize_dask_cache(nbytes=0)
    cache = resize_dask_cache(nbytes=cache_bytes)


def _tray_icon(direction, color, size=16):
    """Upload/download icon: an open tray with an arrow (as fractal-web)."""
    pixmap = QPixmap(size * 2, size * 2)  # drawn at 2x for sharp HiDPI
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 2.2)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    w = size * 2
    # tray
    painter.drawPolyline(QPolygonF([
        QPointF(w * 0.12, w * 0.62), QPointF(w * 0.12, w * 0.86),
        QPointF(w * 0.88, w * 0.86), QPointF(w * 0.88, w * 0.62)]))
    # arrow
    top, bottom = w * 0.12, w * 0.66
    painter.drawLine(QPointF(w / 2, top), QPointF(w / 2, bottom))
    tip, base = (top, top + w * 0.2) if direction == "up" else (bottom, bottom - w * 0.2)
    painter.drawPolyline(QPolygonF([
        QPointF(w / 2 - w * 0.18, base), QPointF(w / 2, tip),
        QPointF(w / 2 + w * 0.18, base)]))
    painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


def _outline_button(text, icon, color, tooltip=""):
    """Outlined button in a given color (like Bootstrap's btn-outline-*)."""
    button = QPushButton(text)
    button.setIcon(icon)
    button.setIconSize(QSize(14, 14))
    button.setToolTip(tooltip)
    button.setCursor(Qt.PointingHandCursor)
    button.setStyleSheet(
        f"QPushButton {{ color: {color}; border: 1px solid {color}; "
        "border-radius: 4px; padding: 3px 10px; background: transparent; }"
        f"QPushButton:hover {{ background: {color}1f; }}"
        f"QPushButton:pressed {{ background: {color}33; }}"
    )
    return button


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
        im_path = str(Path(__file__).parent.absolute()
                      / 'logo_images' / 'fractal_logo.png')
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
        manifest = load_manifest(entry, fallback=info.manifest)
        registered = register_manifest_tasks(
            self.task_manager, manifest, package_key, version,
            entry["src_path"],
        )
        for key in registered.added:
            self.workflow_combo_box.addItem(key)
        if registered.skipped:
            logger.info(
                f"{package_key}: not runnable on viewer images (converters): "
                + ", ".join(registered.skipped)
            )

        selected_key = registered.keys.get(selected_task)
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

        outputs = find_task_outputs(
            args, props, out_json, ui.get('labels_before', set()))
        label_names = outputs.label_names
        image_urls = outputs.image_urls
        missing = outputs.missing
        logger.info(
            f'{task_name}: labels {sorted(label_names)}, images {image_urls}, '
            f'missing {missing}'
        )

        shown = []
        not_loaded = []
        if outputs.input_rewritten:
            # The open layers of the input still show the old data
            added = self._reload_image(path_to_zarr, label_names)
            shown += added
            not_loaded += [f"label '{n}' from {path_to_zarr}"
                           for n in sorted(label_names) if n not in added]
        elif label_names:
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

    def _load_zarr_layers(self, zarr_url, labels=None, suffix=None,
                          images=None):
        """
        Add layers of an OME-Zarr image to the viewer.

        labels: only add these label layers (default: all of them).
        images: add the image layers (default: when no labels are given).
        suffix: name layers "<name> [<suffix>]".
        """
        if images is None:
            images = labels is None
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
            if layer_type == 'labels':
                if labels is not None and meta.get('name') not in labels:
                    continue
            elif not images:
                continue

            # Names may be a list (one per channel)
            names = meta.get('name')
            if suffix and names is not None:
                if isinstance(names, (list, tuple)):
                    names = [f'{n} [{suffix}]' for n in names]
                else:
                    names = f'{names} [{suffix}]'
                meta['name'] = names

            # Replace stale layers with the same name(s) of this image;
            # label layers may have been opened under their path name
            for n in names if isinstance(names, (list, tuple)) else [names]:
                if n is None:
                    continue
                for layer in list(self._viewer.layers):
                    if _from_other_image(layer, zarr_url):
                        continue
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

    def _reload_image(self, zarr_url, labels=()):
        """Read an image again that a task rewrote in place (e.g. Apply
        Registration to Image with overwrite_input): replace its image
        layers and its open label layers, and add the label layers in
        ``labels``. Returns the names of the added layers."""
        old = [layer for layer in self._viewer.layers
               if [os.path.normpath(u) for u in layer_image_urls(layer)]
               == [os.path.normpath(zarr_url)]]
        open_labels = {_label_name(_without_napari_suffix(layer.name))
                       for layer in old
                       if isinstance(layer, napari.layers.Labels)}
        was_selected = self._image_layers.currentText() in {
            layer.name for layer in old}
        for layer in old:
            self._viewer.layers.remove(layer)
        added = self._load_zarr_layers(
            zarr_url, labels=open_labels | set(labels), images=True)
        if was_selected:
            for name in added:
                if isinstance(self._viewer.layers[name], napari.layers.Image):
                    self._image_layers.setCurrentText(name)
                    break
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

    # At most this many new images are opened after a non-parallel/compound run
    MAX_IMAGES_TO_OPEN = 8

    def _viewer_images(self):
        """OME-Zarr images behind the viewer's layers: {zarr_url: [layer names]}."""
        images = {}
        for layer in self._viewer.layers:
            for url in layer_image_urls(layer):
                images.setdefault(url, []).append(layer.name)
        return images

    def _execute_job(self, task_name):
        """Run a non-parallel or compound task on the checked input images."""
        ui = self._task_ui[task_name]
        task = self.task_manager.get_task(task_name)
        kind = task['kind']

        zarr_urls = ui['inputs'].selected()
        if not zarr_urls:
            show_error("Select at least one input image (OME-Zarr loaded in the viewer).")
            return
        zarr_dir = ui['zarr_dir'].text().strip()
        if not zarr_dir:
            show_error("Set the output folder (zarr_dir).")
            return

        try:
            args_p = ui['form'].get_args() if ui.get('form') else {}
            args_np = ui['form_np'].get_args() if ui.get('form_np') else {}
        except SchemaError as e:
            ui['status'].setText(f"⚠ {e}")
            show_error(f"{task['base_name']}: {e}")
            return

        # Kept to find the task's outputs after the run
        ui['args'] = args_np if kind == 'non_parallel' else args_p
        ui['labels_before'] = {u: set(list_labels(u)) for u in zarr_urls}

        package_name = self.task_manager.get_package_name(task_name)
        entry = self.loaded_packages[package_name]
        try:
            pixi = find_pixi()
        except FileNotFoundError as e:
            show_error(str(e))
            return
        run_dir = self.task_manager.get_run_dir(task_name)
        run_dir.mkdir(parents=True, exist_ok=True)
        job = TaskJob(
            kind=kind,
            run_prefix=pixi_run_prefix(pixi, entry),
            executable_parallel=self.task_manager.get_executable_path(task_name, 'parallel'),
            executable_non_parallel=self.task_manager.get_executable_path(task_name, 'non_parallel'),
            zarr_urls=zarr_urls,
            zarr_dir=zarr_dir,
            args_parallel=args_p,
            args_non_parallel=args_np,
            workdir=run_dir,
            stop_on_error=not ui['continue_check'].isChecked(),
        )

        self._update_execute_buttons(is_enabled=False)
        ui['log'].clear()
        ui['progress'].setVisible(True)
        what = short_name(zarr_urls[0]) if len(zarr_urls) == 1 else f"{len(zarr_urls)} images"
        ui['status'].setText(f"Running on {what}…")
        ui['t0'] = time.monotonic()

        # Errors are handled in _on_job_finished; without _ignore_errors
        # napari re-raises them in the main thread
        worker = create_worker(run_task_job, job, _ignore_errors=True)
        worker.yielded.connect(lambda line: self._on_job_log(task_name, line))
        worker.returned.connect(lambda res: self._on_job_finished(task_name, res))
        worker.errored.connect(lambda e: self._on_job_finished(task_name, None, e))
        self._task_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._task_workers.remove(w))
        worker.start()

    def _on_job_log(self, task_name, line):
        ui = self._task_ui.get(task_name)
        if ui is None:
            return
        ui['log'].appendPlainText(line)
        if line.startswith("=== "):  # step headers -> status line
            ui['status'].setText("Running " + line[4:].strip() + "…")

    def _on_job_finished(self, task_name, result, error=None):
        ui = self._task_ui.get(task_name)
        self._update_execute_buttons(is_enabled=True)
        if ui is None:  # tab closed while running
            return
        ui['progress'].setVisible(False)
        elapsed = time.monotonic() - ui.get('t0', time.monotonic())
        name = self.task_manager.get_task(task_name)['base_name']
        if error is None and result is not None:
            failed = f", {len(result.failed)} failed" if result.failed else ""
            n = len(result.processed_zarr_urls)
            ui['status'].setText(
                f"✓ Finished in {elapsed:.0f} s ({n} image{'s' if n != 1 else ''}{failed})"
            )
            if result.failed:
                ui['log_section'].button.setChecked(True)
            self._fetch_job_output(task_name, result)
        else:
            last = str(error) if error else ""
            ui['status'].setText(f"✗ Failed after {elapsed:.0f} s: {last[:200]}")
            ui['log_section'].button.setChecked(True)
            show_error(f"{name} failed: {last[:300]}")

    def _fetch_job_output(self, task_name, result):
        """Show what a non-parallel/compound run wrote: new labels in the
        processed images and the new images in ``image_list_updates``."""
        wipe_cache()
        ui = self._task_ui[task_name]
        before = ui.get('labels_before', {})
        inputs = list(dict.fromkeys(list(before) + result.processed_zarr_urls))
        many = len(inputs) > 1
        props = self.task_manager.get_properties(task_name)

        rewritten = rewritten_inputs(result.image_list_updates, inputs)
        shown = []
        for url in inputs:
            names = new_label_names(
                url, before.get(url, set()), ui.get('args', {}), props)
            if url in rewritten:
                # The open layers of this input still show the old data
                shown += self._reload_image(url, names)
            elif names:
                shown += self._load_zarr_layers(
                    url, labels=names, suffix=short_name(url) if many else None)

        new_urls = new_image_urls(result.image_list_updates, inputs)
        for url in new_urls[: self.MAX_IMAGES_TO_OPEN]:
            shown += self._load_zarr_layers(url, suffix=short_name(url))

        base = self.task_manager.get_task(task_name)['base_name']
        msg = (f"added {', '.join(shown[:6])}" + ("…" if len(shown) > 6 else "")
               if shown else "finished (nothing new to display)")
        if len(new_urls) > self.MAX_IMAGES_TO_OPEN:
            msg += f"; {len(new_urls) - self.MAX_IMAGES_TO_OPEN} more new images not opened"
        show_info(f"{base}: {msg}")

    def _execute_task(self, task_name):
        if self.task_manager.get_task(task_name).get('kind', 'parallel') != 'parallel':
            return self._execute_job(task_name)
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

        path_to_task_args, out_json = self.task_manager.write_args(
            task_name, args)

        # Remember existing labels so new ones can be shown after the run
        ui['labels_before'] = set(list_labels(path_to_zarr))

        package_name = self.task_manager.get_package_name(task_name)
        entry = self.loaded_packages[package_name]
        try:
            pixi = find_pixi()
        except FileNotFoundError as e:
            show_error(str(e))
            return
        cmd = task_command(
            pixi_run_prefix(pixi, entry),
            self.task_manager.get_executable_path(task_name),
            path_to_task_args, out_json,
        )

        self._update_execute_buttons(is_enabled=False)
        ui['log'].clear()
        ui['log'].appendPlainText(f"$ {' '.join(cmd)}\n")
        ui['progress'].setVisible(True)
        ui['status'].setText(f"Running on {os.path.basename(path_to_zarr)}…")
        ui['t0'] = time.monotonic()

        # Errors are handled in _on_task_finished; without _ignore_errors
        # napari re-raises them in the main thread (which aborts with PyQt5)
        worker = create_worker(stream_process, cmd, _ignore_errors=True)
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

    def _channel_suggestions(self, urls=None):
        """Channel labels of the input images (for completers)."""
        urls = urls or [u for u in [self._selected_zarr_url()] if u]
        labels = []
        for url in urls:
            labels += [c for c in channel_labels(url) if c not in labels]
        current = self._image_layers.currentText()
        if current and current not in labels:
            labels.append(current)
        return labels

    def _add_task_tab(self, task_name):
        task = self.task_manager.get_task(task_name)
        schema = self.task_manager.get_schema(task_name)

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
            docs_label = QLabel(docs_summary(docs))
            docs_label.setWordWrap(True)
            docs_label.setToolTip(docs[:3000])
            outer.addWidget(docs_label)
        if task.get('docs_link'):
            link = QLabel(f'<a href="{task["docs_link"]}">Documentation</a>')
            link.setOpenExternalLinks(True)
            outer.addWidget(link)

        kind = task.get('kind', 'parallel')
        input_slots = []
        inputs = zarr_dir_edit = None
        if kind == 'parallel':
            input_label = QLabel()
            input_label.setStyleSheet("color: gray;")

            def _update_input(*_):
                name = self._image_layers.currentText()
                input_label.setText(f"Input: {name}" if name else "Input: (select an image in the Main tab)")
            _update_input()
            self._image_layers.currentTextChanged.connect(_update_input)
            input_slots.append(_update_input)
            outer.addWidget(input_label)
        else:
            hint = QLabel({
                'non_parallel': "Runs once on all selected images together.",
                'compound': "A setup step runs once on all selected images, "
                            "then the compute step runs once per image it returns.",
            }[kind])
            hint.setStyleSheet("color: gray;")
            hint.setWordWrap(True)
            outer.addWidget(hint)

            # Input images: OME-Zarrs loaded in the viewer
            # A plate/well layer stands for all the images inside it
            selected_url = self._selected_zarr_url()
            selected_urls = image_urls(selected_url)
            inputs = _InputImages(
                self._viewer_images,
                default=lambda url: url in selected_urls or selected_url is None,
            )
            self._viewer.layers.events.inserted.connect(inputs.refresh)
            self._viewer.layers.events.removed.connect(inputs.refresh)
            outer.addWidget(_Collapsible("Input images", inputs, expanded=True))

            # Fractal's zarr_dir: where new OME-Zarrs are written
            zarr_dir_edit = QLineEdit()
            zarr_dir_edit.setToolTip(
                "Fractal's zarr_dir: folder containing the plates/images; "
                "new OME-Zarrs are written here.")
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(QLabel("Output folder (zarr_dir):"))
            row_layout.addWidget(zarr_dir_edit, 1)
            outer.addWidget(row)
            edited = {'by_user': False}
            zarr_dir_edit.textEdited.connect(lambda *_: edited.update(by_user=True))

            def _update_zarr_dir(*_):
                if not edited['by_user']:
                    zarr_dir_edit.setText(default_zarr_dir(inputs.selected()))
            inputs.changed.connect(_update_zarr_dir)
            _update_zarr_dir()

        # Parameters
        channels = self._channel_suggestions(inputs.selected() if inputs else None)
        suggestions = {k: channels for k in CHANNEL_FIELDS}
        autofills = []

        def _make_form(s):
            f = SchemaForm(with_defs(s), suggestions=suggestions)
            autofills.append(ChannelAutofill(f.fields))
            return f

        def _update_channel(*_):
            url = self._selected_zarr_url()
            labels = channel_labels(url) if url else []
            channel = _input_channel(self._image_layers.currentText(), labels)
            for autofill in autofills:
                autofill.set_channel(channel)

        form = form_np = None
        if kind == 'parallel':
            form = _make_form(schema)
            params = form
        else:
            params = QWidget()
            params_layout = QVBoxLayout(params)
            params_layout.setContentsMargins(0, 0, 0, 0)
            if kind == 'compound':
                form_np = _make_form(task['schema_non_parallel'])
                form = _make_form(task['schema_parallel'])
                params_layout.addWidget(_Collapsible(
                    "Setup (runs once on all images)", form_np,
                    expanded=bool(form_np.fields)))
                params_layout.addWidget(_Collapsible(
                    "Compute (runs per image)", form, expanded=True))
            else:
                form_np = _make_form(schema)
                params_layout.addWidget(form_np)
            params_layout.addStretch(1)
        _update_channel()
        self._image_layers.currentTextChanged.connect(_update_channel)
        input_slots.append(_update_channel)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(params)
        outer.addWidget(scroll, 1)

        # Import/export arguments (Fractal's arguments file format)
        args_row = QWidget()
        args_layout = QHBoxLayout(args_row)
        args_layout.setContentsMargins(0, 0, 0, 0)
        import_btn = _outline_button(
            "Import", _tray_icon("up", "#0d6efd"), "#0d6efd",
            "Load arguments from a Fractal arguments file (fractal-web "
            "'Export') or a fractal-server workflow export")
        import_btn.clicked.connect(lambda: self._import_args(task_name))
        export_btn = _outline_button(
            "Export", _tray_icon("down", "#6c757d"), "#6c757d",
            "Save the arguments as a Fractal arguments file "
            "(args-<task>.json, as fractal-web's 'Export')")
        export_btn.clicked.connect(
            lambda: self._export_args(task_name, workflow=False))
        args_layout.addWidget(import_btn)
        args_layout.addWidget(export_btn)
        args_layout.addStretch(1)
        outer.addWidget(args_row)

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
        reset_btn.clicked.connect(
            lambda: [f.reset() for f in (form, form_np) if f is not None])
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

        continue_check = QCheckBox("Continue with the other images if one fails")
        continue_check.setVisible(kind == 'compound')
        outer.addWidget(continue_check)

        status = QLabel("")
        status.setWordWrap(True)
        outer.addWidget(status)

        log = QPlainTextEdit()
        log.setReadOnly(True)
        log.setMaximumBlockCount(5000)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSize(9)
        log.setFont(mono)
        log.setMinimumHeight(160)
        log_section = _Collapsible("Log", log, expanded=False)
        outer.addWidget(log_section)

        self.exec_btn_dict[task_name] = run_btn
        self._task_ui[task_name] = dict(
            page=page, form=form, form_np=form_np, run_btn=run_btn,
            progress=progress, status=status, log=log, log_section=log_section,
            input_slots=tuple(input_slots), inputs=inputs,
            zarr_dir=zarr_dir_edit, continue_check=continue_check,
        )

        short = task['base_name'] if len(task['base_name']) <= 22 else task['base_name'][:21] + '…'
        idx = self.tab_container.addTab(page, short)
        self.tab_container.setTabToolTip(idx, task_name)
        self.tab_container.setCurrentIndex(idx)

    # ------------------------------------------------------------------
    # Import / export of arguments (Fractal formats)
    # ------------------------------------------------------------------

    def _task_identity(self, task_name):
        """(task name, package, version) as Fractal names them."""
        task = self.task_manager.get_task(task_name)
        pkg, _, pkg_version = task['package_name'].partition('==')
        return task['base_name'], pkg, task.get('version') or pkg_version

    def _args_dir(self):
        return getattr(self, '_last_args_dir', None) or str(Path.home())

    def _export_args(self, task_name, workflow=False):
        ui = self._task_ui[task_name]
        name, pkg, version = self._task_identity(task_name)
        try:
            # All current values (defaults included), like fractal-web; a
            # required argument that is still empty is simply left out
            args_p = ui['form'].body.values(omit_defaults=False) if ui.get('form') else None
            args_np = ui['form_np'].body.values(omit_defaults=False) if ui.get('form_np') else None
        except SchemaError as e:
            show_error(f"Cannot export the arguments of {name}: {e}")
            return
        if workflow:
            data = export_workflow(name, pkg, version, args_p, args_np)
            slug = re.sub(r"\s+", "-", name.strip().lower())
            default = f"workflow-{slug}.json"
            title = "Export a Fractal workflow with this task"
        else:
            data = export_args(args_p, args_np)
            default = args_filename(name)
            title = "Export task arguments"
        path, _ = QFileDialog.getSaveFileName(
            self, title, os.path.join(self._args_dir(), default),
            "JSON files (*.json)")
        if not path:  # cancelled
            return
        try:
            write_json(path, data)
        except OSError as e:
            show_error(f"Could not write {path}: {e}")
            return
        self._last_args_dir = os.path.dirname(path)
        ui['status'].setText(f"Exported arguments to {path}")
        show_info(f"{name}: exported arguments to {os.path.basename(path)}")

    def _import_args(self, task_name, path=None):
        ui = self._task_ui[task_name]
        name, pkg, version = self._task_identity(task_name)
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import task arguments", self._args_dir(),
                "JSON files (*.json);;All files (*)")
            if not path:  # cancelled
                return
        try:
            imported = read_args(path, task_name=name, pkg_name=pkg,
                                 version=version)
        except (OSError, ValueError) as e:
            show_error(f"Cannot import arguments for {name}: {e}")
            return
        self._last_args_dir = os.path.dirname(path)

        notes = list(imported.notes)
        applied = 0
        for form, args, step in (
            (ui.get('form'), imported.args_parallel, 'args_parallel'),
            (ui.get('form_np'), imported.args_non_parallel, 'args_non_parallel'),
        ):
            if args is None:
                continue
            if form is None:
                if args:
                    notes.append(f"{step} not used by this task")
                continue
            form.reset()
            unknown = []
            for key, value in args.items():
                if key not in form.fields:
                    unknown.append(key)
                    continue
                try:
                    form.fields[key].set_value(value)
                    applied += 1
                except Exception:  # noqa: BLE001 - value of the wrong type
                    notes.append(f"could not set {key} to {value!r}")
            if unknown:
                notes.append("unknown arguments ignored: " + ", ".join(unknown))

        errors = []
        for form in (ui.get('form_np'), ui.get('form')):
            if form is not None:
                errors += form.validate()
        msg = f"Imported {applied} argument{'s' if applied != 1 else ''} from {os.path.basename(path)}"
        if notes:
            msg += " (" + "; ".join(notes) + ")"
        if errors:
            msg += f". Check: {'; '.join(str(e) for e in errors[:3])}"
        ui['status'].setText(msg)
        (show_warning if notes or errors else show_info)(f"{name}: {msg}")

    def _close_tab(self, task_name):
        ui = self._task_ui.pop(task_name, None)
        self.exec_btn_dict.pop(task_name, None)
        if ui is not None:
            for slot in ui['input_slots']:
                self._image_layers.currentTextChanged.disconnect(slot)
            if ui.get('inputs') is not None:
                for event in (self._viewer.layers.events.inserted,
                              self._viewer.layers.events.removed):
                    try:
                        event.disconnect(ui['inputs'].refresh)
                    except (TypeError, ValueError, RuntimeError):
                        pass
            idx = self.tab_container.indexOf(ui['page'])
            if idx >= 0:
                self.tab_container.removeTab(idx)
            ui['page'].deleteLater()

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

        # Create ROIs from shape layer polygons and save them to a table
        write_roi_table(image_layer.source.path, shapes_layer.data,
                        table_name, overwrite=overwrite)

        # Just for testing
        # ome_zarr = open_ome_zarr_container(image_layer.source.path)
        # roi_table_crops = ome_zarr.get_roi_table(table_name)
        # imgs = ome_zarr.get_image("0")
        # pixel_sizes = imgs.pixel_size.zyx
        # for roi in roi_table_crops.rois():
        #     cropped_img = imgs.get_roi_as_numpy(roi, c=0)
        #     # add to viewer
        #     self._viewer.add_image(cropped_img, name=f"Cropped_FOV_ROI_{roi.name}",
        #                            scale=pixel_sizes, blending="additive")

        logger.info(f"Finished cropping {image_name} to ROI(s).")
