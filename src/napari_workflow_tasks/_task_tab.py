"""
The tab of one task: its input images, its arguments, running it with a live
log, and showing what it wrote in the viewer.
"""

import os
import re
import time
from pathlib import Path

from napari.layers import Shapes
from napari.qt.threading import create_worker
from napari.utils.notifications import show_error, show_info, show_warning
from qtpy.QtCore import QPointF, QSize, Qt, Signal
from qtpy.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from workflow_tasks_core import SchemaError, Task
from workflow_tasks_core.args_io import args_filename
from workflow_tasks_core.manifests import docs_summary
from workflow_tasks_core.rois import write_roi_table
from workflow_tasks_core.runner import default_zarr_dir, short_name
from workflow_tasks_core.zarr_meta import channel_labels

from ._autofill import CHANNEL_FIELDS, ChannelAutofill, input_channel
from ._schema_form import (
    ObjectField,
    OptionalField,
    SchemaForm,
    StringField,
    _Collapsible,
)
from ._show import image_layer_names, layer_image_url, show_run

# At most this many new images are opened after a run
MAX_IMAGES_TO_OPEN = 8
# Arguments naming the ROI table a task works on (lower case), also inside
# argument models: e.g. input_roi_table (fractal-tasks-core < 1.0:
# input_ROI_table), iterator_configuration.roi_table ("Iterate Over ROIs")
ROI_TABLE_ARGS = ("input_roi_table", "roi_table")


class _LayerCombo(QComboBox):
    """Dropdown of the image layers of OME-Zarr images in the viewer. Keeps
    the layer chosen last, also while it is gone (a task reloading an image
    removes its layers and adds new ones with the same names)."""

    def __init__(self, viewer, chosen=""):
        super().__init__()
        self._viewer = viewer
        self._chosen = chosen
        self.activated.connect(lambda _index: self._remember())
        self.refresh()

    def _remember(self):
        self._chosen = self.currentText()

    def choose(self, layer_name):
        self.setCurrentText(layer_name)
        self._remember()

    def refresh(self, *_):
        before = self.currentText()
        if not self._chosen:  # keep the one shown now
            self._chosen = before
        names = image_layer_names(self._viewer)
        self.blockSignals(True)
        self.clear()
        self.addItems(names)
        if self._chosen in names:
            self.setCurrentText(self._chosen)
        self.blockSignals(False)
        if self.currentText() != before:
            self.currentTextChanged.emit(self.currentText())

    def image_url(self):
        """The OME-Zarr image of the chosen layer, or None."""
        name = self.currentText()
        if name not in self._viewer.layers:
            return None
        return layer_image_url(self._viewer.layers[name])


class InputLayers(QWidget):
    """A task's input: one dropdown per image. Tasks that run on several
    images together (``several=True``) can add and remove dropdowns."""

    changed = Signal()

    def __init__(self, viewer, several, parent=None):
        super().__init__(parent)
        self._viewer = viewer
        self._several = several
        self._rows = []  # (row widget, dropdown, remove button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout = QVBoxLayout()
        layout.addLayout(self._rows_layout)
        self._no_images = QLabel("(open an OME-Zarr image in napari)")
        self._no_images.setStyleSheet("color: gray;")
        layout.addWidget(self._no_images)
        if several:
            add_button = QPushButton("+ Add image")
            add_button.clicked.connect(self.add_row)
            layout.addWidget(add_button, alignment=Qt.AlignLeft)
        self.add_row()

    def add_row(self):
        combo = _LayerCombo(self._viewer, chosen=self._next_layer())
        combo.currentTextChanged.connect(lambda _name: self.changed.emit())
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(combo, 1)
        remove_button = None
        if self._several:
            remove_button = QPushButton("✕")
            remove_button.setFixedWidth(28)
            remove_button.setToolTip("Remove this image")
            remove_button.clicked.connect(lambda: self._remove_row(row))
            row_layout.addWidget(remove_button)
        self._rows.append((row, combo, remove_button))
        self._rows_layout.addWidget(row)
        self._update()
        self.changed.emit()

    def _remove_row(self, row):
        self._rows = [r for r in self._rows if r[0] is not row]
        row.deleteLater()
        self._update()
        self.changed.emit()

    def _next_layer(self):
        """A layer of an image no dropdown shows yet ("": the first)."""
        selected = self.selected()
        for name in image_layer_names(self._viewer):
            if layer_image_url(self._viewer.layers[name]) not in selected:
                return name
        return ""

    def _update(self):
        self._no_images.setVisible(not image_layer_names(self._viewer))
        for _row, _combo, remove_button in self._rows:
            if remove_button is not None:
                remove_button.setEnabled(len(self._rows) > 1)

    def refresh(self, *_):
        for _row, combo, _remove in self._rows:
            combo.refresh()
        self._update()

    def combos(self):
        return [combo for _row, combo, _remove in self._rows]

    def layers(self):
        """Names of the chosen layers."""
        return [combo.currentText() for combo in self.combos()]

    def selected(self):
        """The images of the chosen layers (each once)."""
        urls = [combo.image_url() for combo in self.combos()]
        return list(dict.fromkeys(url for url in urls if url))


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


def _set_roi_table(fields, table_name, prefix=""):
    """Set the ROI table arguments among ``fields`` (a form's fields) to
    ``table_name``, also inside argument models; an optional model holding
    one is switched on. Returns the paths of the arguments set."""
    filled = []
    for key, field in fields.items():
        path = prefix + key
        if key.lower() in ROI_TABLE_ARGS and isinstance(field, StringField):
            field.set_value(table_name)
            filled.append(path)
            continue
        model = field.inner if isinstance(field, OptionalField) else field
        if isinstance(model, ObjectField):
            found = _set_roi_table(model.form.fields, table_name, path + ".")
            if found and isinstance(field, OptionalField):
                field.toggle.setChecked(True)
            filled += found
    return filled


def _lines(lines):
    """Generator function around ``lines``: napari's ``create_worker`` only
    streams the values of generator functions."""
    yield from lines


class TaskTab(QWidget):
    """Tab of one task: its input images, an optional ROI table drawn in
    the viewer, its arguments, its log. The channel fields follow the
    channel of the first input layer.

    Tasks run one at a time (they may write to the same OME-Zarr): the
    widget blocks the other tabs (``set_blocked``) between ``run_started``
    and ``run_finished``."""

    remove_requested = Signal()
    run_started = Signal()
    run_finished = Signal()

    # Folder of the latest imported/exported arguments file (all tabs)
    last_args_dir = str(Path.home())

    def __init__(self, task: Task, viewer, parent=None):
        super().__init__(parent)
        self.task = task
        self._viewer = viewer
        self._worker = None
        self._started = 0.0
        self._blocked = False  # another task is running

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self._add_header(layout)

        # Input images
        if task.kind == "parallel":
            hint = "Runs on the image of the input layer."
        elif task.kind == "non_parallel":
            hint = "Runs once on all input images together."
        else:
            hint = ("A setup step runs once on all input images, then the "
                    "compute step runs once per image it returns.")
        layout.addWidget(self._gray_label(hint))
        self.inputs = InputLayers(viewer, several=task.kind != "parallel")
        viewer.layers.events.inserted.connect(self.inputs.refresh)
        viewer.layers.events.removed.connect(self.inputs.refresh)
        layout.addWidget(_Collapsible("Input", self.inputs, expanded=True))

        # Fractal's zarr_dir: where new OME-Zarrs are written
        self.zarr_dir = None
        self._zarr_dir_edited = False
        if task.kind != "parallel":
            self.zarr_dir = QLineEdit()
            self.zarr_dir.setToolTip(
                "Fractal's zarr_dir: folder containing the plates/images; "
                "new OME-Zarrs are written here.")
            self.zarr_dir.textEdited.connect(self._on_zarr_dir_edited)
            row = QHBoxLayout()
            row.addWidget(QLabel("Output folder (zarr_dir):"))
            row.addWidget(self.zarr_dir, 1)
            layout.addLayout(row)
            self._update_zarr_dir()
        self.inputs.changed.connect(self._on_inputs_changed)

        self._add_roi_section(layout)
        self._add_forms(layout)
        self._add_buttons(layout)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # busy indicator
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.continue_check = QCheckBox("Continue with the other images if one fails")
        self.continue_check.setVisible(task.kind == "compound")
        layout.addWidget(self.continue_check)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSize(9)
        self.log.setFont(mono)
        self.log.setMinimumHeight(160)
        self.log_section = _Collapsible("Log", self.log, expanded=False)
        layout.addWidget(self.log_section)

        self._update_channel()
        self._update_run_button()

    # ------------------------------------------------------------------
    # Building the tab
    # ------------------------------------------------------------------

    @staticmethod
    def _gray_label(text):
        label = QLabel(text)
        label.setStyleSheet("color: gray;")
        label.setWordWrap(True)
        return label

    def _add_header(self, layout):
        """Name, package and version, short docs."""
        title = QLabel(self.task.name)
        title.setFont(QFont("Arial", 14, weight=QFont.Bold))
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self._gray_label(self.task.package.key))
        docs = self.task.docs_info.strip()
        if docs:
            docs_label = QLabel(docs_summary(docs))
            docs_label.setWordWrap(True)
            docs_label.setToolTip(docs[:3000])
            layout.addWidget(docs_label)
        if self.task.docs_link:
            link = QLabel(f'<a href="{self.task.docs_link}">Documentation</a>')
            link.setOpenExternalLinks(True)
            layout.addWidget(link)

    def _add_roi_section(self, layout):
        """Draw ROIs in a Shapes layer and save them as an ROI table of the
        input images, used as the task's ROI table."""
        content = QWidget()
        roi_layout = QVBoxLayout(content)
        roi_layout.setContentsMargins(0, 0, 0, 0)
        add_shapes_button = QPushButton("Add Shapes layer")
        add_shapes_button.setToolTip("Draw the ROIs in it (e.g. as rectangles "
                                     "or polygons)")
        add_shapes_button.clicked.connect(self._add_shapes_layer)
        roi_layout.addWidget(add_shapes_button)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.roi_table_name = QLineEdit("Cropped_FOV_ROI_table")
        name_row.addWidget(self.roi_table_name, 1)
        self.roi_overwrite = QCheckBox("Overwrite")
        name_row.addWidget(self.roi_overwrite)
        roi_layout.addLayout(name_row)
        save_button = QPushButton("Save ROIs to table")
        save_button.setToolTip(
            "Save the shapes of the newest Shapes layer (their bounding "
            "boxes) as an ROI table of each input image, and use it as this "
            "task's ROI table (e.g. Input ROI Table, Iterate Over ROIs)")
        save_button.clicked.connect(self.save_rois)
        roi_layout.addWidget(save_button)
        layout.addWidget(_Collapsible("ROI table (optional)", content,
                                      expanded=False))

    def _add_forms(self, layout):
        """``form``: the per-image arguments (parallel step); ``form_np``:
        those of the non-parallel step (setup step of a compound task)."""
        channels = self._channel_suggestions()
        suggestions = dict.fromkeys(CHANNEL_FIELDS, channels)
        self._autofills = []

        def make_form(step):
            form = SchemaForm(self.task.args_schema(step) or {},
                              suggestions=suggestions)
            self._autofills.append(ChannelAutofill(form.fields))
            return form

        self.form = self.form_np = None
        if self.task.kind == "parallel":
            self.form = params = make_form("parallel")
        elif self.task.kind == "non_parallel":
            self.form_np = params = make_form("non_parallel")
        else:
            self.form_np = make_form("non_parallel")
            self.form = make_form("parallel")
            params = QWidget()
            params_layout = QVBoxLayout(params)
            params_layout.setContentsMargins(0, 0, 0, 0)
            params_layout.addWidget(_Collapsible(
                "Setup (runs once on all images)", self.form_np,
                expanded=bool(self.form_np.fields)))
            params_layout.addWidget(_Collapsible(
                "Compute (runs per image)", self.form, expanded=True))
            params_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(params)
        layout.addWidget(scroll, 1)

    def _add_buttons(self, layout):
        # Import/export of arguments (Fractal's arguments file format)
        args_row = QHBoxLayout()
        import_button = _outline_button(
            "Import", _tray_icon("up", "#0d6efd"), "#0d6efd",
            "Load arguments from a Fractal arguments file (fractal-web "
            "'Export') or a fractal-server workflow export")
        import_button.clicked.connect(lambda: self.import_args())
        export_button = _outline_button(
            "Export", _tray_icon("down", "#6c757d"), "#6c757d",
            "Save the arguments as a Fractal arguments file "
            "(args-<task>.json, as fractal-web's 'Export')")
        export_button.clicked.connect(lambda: self.export_args())
        args_row.addWidget(import_button)
        args_row.addWidget(export_button)
        args_row.addStretch(1)
        layout.addLayout(args_row)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("▶ Run")
        self.run_button.setDefault(True)
        self.run_button.setStyleSheet("font-weight: bold;")
        self.run_button.clicked.connect(self.run)
        reset_button = QPushButton("Reset")
        reset_button.setToolTip("Reset all parameters to the task defaults")
        reset_button.clicked.connect(self.reset)
        self.remove_button = QPushButton("Remove task")
        self.remove_button.clicked.connect(self.remove_requested.emit)
        buttons.addWidget(self.run_button, 2)
        buttons.addWidget(reset_button, 1)
        buttons.addWidget(self.remove_button, 1)
        layout.addLayout(buttons)

    def _forms(self):
        return [form for form in (self.form_np, self.form) if form is not None]

    def reset(self):
        for form in self._forms():
            form.reset()

    def disconnect_viewer(self):
        """Stop following the viewer's layers (before removing the tab)."""
        for event in (self._viewer.layers.events.inserted,
                      self._viewer.layers.events.removed):
            event.disconnect(self.inputs.refresh)

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def _channel_suggestions(self):
        """Channel labels of the input images (for completers)."""
        labels = []
        for url in self.inputs.selected():
            labels += [c for c in channel_labels(url) if c not in labels]
        for name in self.inputs.layers():
            if name and name not in labels:
                labels.append(name)
        return labels

    def _update_channel(self):
        """Fill the channel fields with the channel of the first input
        layer."""
        first = self.inputs.combos()[0] if self.inputs.combos() else None
        if first is None:
            return
        url = first.image_url()
        channels = channel_labels(url) if url else []
        channel = input_channel(first.currentText(), channels)
        for autofill in self._autofills:
            autofill.set_channel(channel)

    def _on_inputs_changed(self):
        self._update_zarr_dir()
        self._update_run_button()
        self._update_channel()

    def _on_zarr_dir_edited(self, *_):
        self._zarr_dir_edited = True

    def _update_zarr_dir(self):
        if self.zarr_dir is not None and not self._zarr_dir_edited:
            self.zarr_dir.setText(default_zarr_dir(self.inputs.selected()))

    def set_blocked(self, blocked):
        """Disable running while another task runs."""
        self._blocked = blocked
        self._update_run_button()

    def _update_run_button(self):
        running = self._worker is not None
        self.run_button.setEnabled(
            not running and not self._blocked and bool(self.inputs.selected()))
        self.remove_button.setEnabled(not running)

    # ------------------------------------------------------------------
    # ROI table
    # ------------------------------------------------------------------

    def _add_shapes_layer(self):
        self._viewer.add_layer(Shapes(name="ROI selection",
                                      shape_type="polygon"))

    def save_rois(self):
        """Save the shapes of the newest Shapes layer as an ROI table of
        each input image and fill the task's ROI table argument."""
        table_name = self.roi_table_name.text().strip()
        if not table_name:
            show_error("Enter a name for the ROI table.")
            return
        urls = self.inputs.selected()
        if not urls:
            show_error("Choose an OME-Zarr image layer as input first.")
            return
        shapes = [layer for layer in self._viewer.layers
                  if isinstance(layer, Shapes) and len(layer.data)]
        if not shapes:
            show_error("Draw the ROIs in a Shapes layer first.")
            return
        if len(shapes) > 1:
            show_warning(f"Several Shapes layers: using '{shapes[-1].name}'.")
        for url in urls:
            try:
                write_roi_table(url, shapes[-1].data, table_name,
                                overwrite=self.roi_overwrite.isChecked())
            except Exception as e:  # noqa: BLE001 - ngio's errors
                show_error(f"Could not save the ROI table in "
                           f"{short_name(url)}: {e}")
                return
        filled = self._use_roi_table(table_name)
        message = (f"Saved {len(shapes[-1].data)} ROI(s) as table "
                   f"'{table_name}' of {', '.join(map(short_name, urls))}")
        if filled:
            message += f"; set {', '.join(filled)}"
        self.status.setText(message)
        show_info(f"{self.task.name}: {message}")

    def _use_roi_table(self, table_name):
        """Set the task's ROI table arguments to ``table_name``; returns
        their paths (e.g. "iterator_configuration.roi_table")."""
        filled = []
        for form in self._forms():
            filled += _set_roi_table(form.fields, table_name)
        return filled

    # ------------------------------------------------------------------
    # Running (in the background, with a live log)
    # ------------------------------------------------------------------

    def run(self):
        task = self.task
        try:
            args = self.form.get_args() if self.form else {}
            args_np = self.form_np.get_args() if self.form_np else None
        except SchemaError as e:
            self.status.setText(f"⚠ {e}")
            show_error(f"{task.name}: {e}")
            return
        zarr_dir = self.zarr_dir.text().strip() if self.zarr_dir else None
        try:
            lines = task.run_lines(
                self.inputs.selected(), args, non_parallel_args=args_np,
                zarr_dir=zarr_dir or None,
                stop_on_error=not self.continue_check.isChecked())
        except (SchemaError, FileNotFoundError, RuntimeError, ValueError) as e:
            self.status.setText(f"⚠ {e}")
            show_error(f"{task.name}: {e}")
            return

        self.log.clear()
        self.progress.setVisible(True)
        inputs = self.inputs.selected()
        what = short_name(inputs[0]) if len(inputs) == 1 else f"{len(inputs)} images"
        self.status.setText(f"Running on {what}…")
        self._started = time.monotonic()

        # Errors are handled in _on_finished; without _ignore_errors napari
        # re-raises them in the main thread
        self._worker = create_worker(_lines, lines, _ignore_errors=True)
        self._worker.yielded.connect(self._on_log)
        self._worker.returned.connect(lambda _: self._on_finished())
        self._worker.errored.connect(self._on_finished)
        self._update_run_button()
        self.run_started.emit()
        self._worker.start()

    def _on_log(self, line):
        self.log.appendPlainText(line)
        if line.startswith("=== "):  # step headers -> status line
            self.status.setText("Running " + line[4:].strip() + "…")

    def _on_finished(self, error=None):
        self._worker = None
        self._update_run_button()
        self.run_finished.emit()
        self.progress.setVisible(False)
        elapsed = time.monotonic() - self._started
        if error is not None:
            message = str(error)
            self.status.setText(f"✗ Failed after {elapsed:.0f} s: {message[:200]}")
            self.log_section.button.setChecked(True)
            show_error(f"{self.task.name} failed: {message[:300]}")
            return
        failed = self.task.failed
        self.status.setText(f"✓ Finished in {elapsed:.0f} s"
                            + (f", {len(failed)} image(s) failed" if failed else ""))
        if failed:
            self.log_section.button.setChecked(True)
        self._show_outputs()

    def _show_outputs(self):
        """Add what the run wrote to the viewer and say what it was."""
        shown = show_run(self._viewer, self.task, open_inputs=False,
                         max_new_images=MAX_IMAGES_TO_OPEN)
        name = self.task.name
        problems = []
        if self.task.missing:
            problems.append("did not write the expected output: "
                            + "; ".join(self.task.missing))
        if shown.not_loaded:
            problems.append("could not load " + "; ".join(shown.not_loaded))
        if shown.added:
            message = f"{name}: added {', '.join(shown.added[:6])}"
            if len(shown.added) > 6:
                message += "…"
            if shown.not_opened:
                message += f"; {shown.not_opened} more new images not opened"
            show_info(message)
        if problems:
            show_warning(f"{name} finished, but {' and '.join(problems)}")
        elif not shown.added:
            show_info(f"{name} finished (it has no label or image output to display).")

    # ------------------------------------------------------------------
    # Import / export of arguments (Fractal formats)
    # ------------------------------------------------------------------

    def export_args(self, workflow=False):
        name = self.task.name
        try:
            # All current values (defaults included), like fractal-web; a
            # required argument that is still empty is simply left out
            args = self.form.body.values(omit_defaults=False) if self.form else {}
            args_np = (self.form_np.body.values(omit_defaults=False)
                       if self.form_np else None)
        except SchemaError as e:
            show_error(f"Cannot export the arguments of {name}: {e}")
            return
        if workflow:
            slug = re.sub(r"\s+", "-", name.strip().lower())
            default = f"workflow-{slug}.json"
            title = "Export a Fractal workflow with this task"
        else:
            default = args_filename(name)
            title = "Export task arguments"
        path, _ = QFileDialog.getSaveFileName(
            self, title, os.path.join(TaskTab.last_args_dir, default),
            "JSON files (*.json)")
        if not path:  # cancelled
            return
        try:
            self.task.write_args(path, args, non_parallel_args=args_np,
                                 workflow=workflow)
        except OSError as e:
            show_error(f"Could not write {path}: {e}")
            return
        TaskTab.last_args_dir = os.path.dirname(path)
        self.status.setText(f"Exported arguments to {path}")
        show_info(f"{name}: exported arguments to {os.path.basename(path)}")

    def import_args(self, path=None):
        name = self.task.name
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import task arguments", TaskTab.last_args_dir,
                "JSON files (*.json);;All files (*)")
            if not path:  # cancelled
                return
        try:
            imported = self.task.read_args(path)
        except (OSError, ValueError) as e:
            show_error(f"Cannot import arguments for {name}: {e}")
            return
        TaskTab.last_args_dir = os.path.dirname(path)

        notes = list(imported.notes)
        applied = 0
        for form, args, step in (
            (self.form, imported.args_parallel, "args_parallel"),
            (self.form_np, imported.args_non_parallel, "args_non_parallel"),
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
        for form in self._forms():
            errors += form.validate()
        message = (f"Imported {applied} argument{'s' if applied != 1 else ''} "
                   f"from {os.path.basename(path)}")
        if notes:
            message += " (" + "; ".join(notes) + ")"
        if errors:
            message += f". Check: {'; '.join(str(e) for e in errors[:3])}"
        self.status.setText(message)
        (show_warning if notes or errors else show_info)(f"{name}: {message}")
