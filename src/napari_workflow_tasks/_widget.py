# from superqt import QCollapsible
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import napari
from napari.qt.threading import thread_worker
from napari_ome_zarr._reader import napari_get_reader
from qtpy.QtCore import QSize, Qt
from qtpy.QtGui import QFont, QPixmap
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .io_utils import PackageImporter

if TYPE_CHECKING:
    import napari

# from io_utils import PackageImporter

# TODO: Automatically decide what properties to ignore based on MANIFEST
IGNORE_PROPERTIES = [
    "zarr_url",
    "channels_to_include",
    "channels_to_exclude",
    "measure_texture",
]  # , 'channel'
INCLUDE_CATEGORIES = ["Segmentation", "Measurement"]


def wipe_cache():
    from napari.utils import resize_dask_cache

    cache = resize_dask_cache()
    cache_bytes = cache.cache.available_bytes
    cache = resize_dask_cache(nbytes=0)
    cache = resize_dask_cache(nbytes=cache_bytes)


def abspath(root, relpath):
    root = Path(root)
    if root.is_dir():
        path = root / relpath
    else:
        path = root.parent / relpath
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
        pixi_manifest_path,
    ):
        task_dict = dict(
            title=title,
            package_name=package_name,
            parent_dir=parent_dir,
            executable_parallel=executable_parallel,
            properties=properties,
            defs=defs,
            required=required,
            type=type,
            pixi_manifest_path=pixi_manifest_path,
            widget_dict=dict(),
        )
        self.tasks[name] = task_dict

    def get_executable_path(self, name):
        parent_dir = self.tasks[name]["parent_dir"]
        exec_fname = self.tasks[name]["executable_parallel"]

        return os.path.join(parent_dir, exec_fname)

    def get_package_name(self, name):
        return self.tasks[name]["package_name"]

    def get_parent_dir(self, name):
        return self.tasks[name]["parent_dir"]

    def get_pixi_manifest_path(self, name):
        return self.tasks[name]["pixi_manifest_path"]

    def get_path_to_json(self, name):
        parent_dir = self.tasks[name]["parent_dir"]
        title = self.tasks[name]["title"]
        return os.path.join(parent_dir, f"{title}.json")

    def get_task(self, name):
        return self.tasks[name]

    def get_properties(self, name):
        return self.tasks[name]["properties"]

    def get_defs(self, name):
        return self.tasks[name]["defs"]

    def write_to_json(self, name):
        # Write json to ".napari_workflow_tasks" directory inside "params" directory
        parent_dir = self.tasks[name]["parent_dir"]
        title = self.tasks[name]["title"]
        path_to_json = os.path.join(parent_dir, f"{title}.json")

        args_dict = dict()
        for prop_key in self.tasks[name]["properties"].keys():
            if "value" in self.tasks[name]["properties"][prop_key]:
                args_dict[prop_key] = self.tasks[name]["properties"][prop_key][
                    "value"
                ]

        with open(path_to_json, "w") as f:
            json.dump(args_dict, f)

    def get_title(self, name):
        return self.tasks[name]["title"]

    def update_task_property(self, name, property, value):
        print("Property dict updated", name, property, value)
        try:
            self.tasks[name]["properties"][property]["value"] = value
        except KeyError:
            print(f"Property {property} not defined in MANIFEST")

    def add_widget_dict(self, name, widget_dict):
        self.tasks[name]["widget_dict"] = widget_dict

    def remove_widget_dict(self, name):
        self.tasks[name]["widge_dict"] = dict()

    def get_widget_value(self, name, property):
        widget = self.tasks[name]["widget_dict"][property]

        if isinstance(widget, QLineEdit):
            value = widget.text()
            if value == "":
                return None
            else:
                try:
                    type = self.tasks[name]["properties"][property]["type"]

                    if type == "integer":
                        return int(value)
                    elif type == "float":
                        return float(value)
                    else:
                        return value
                except KeyError:
                    return value

        elif isinstance(widget, QCheckBox):
            if widget.isChecked():
                return True
            else:
                return False

        elif isinstance(widget, dict):
            args_dict = dict()
            ref = os.path.split(
                self.tasks[name]["properties"][property]["$ref"]
            )[-1]
            for key in widget.keys():
                if isinstance(widget[key], QLineEdit):
                    value = widget[key].text()
                    if value == "":
                        args_dict[key] = None
                    else:
                        try:
                            type = self.tasks[name]["defs"][ref][key]["type"]
                            print(name, property, key, type)

                            if type == "integer":
                                args_dict[key] = int(value)
                            elif type == "float":
                                args_dict[key] = float(value)
                            else:
                                args_dict[key] = value
                        except KeyError:
                            args_dict[key] = value

                elif isinstance(widget[key], QCheckBox):
                    if widget[key].isChecked():
                        args_dict[key] = True
                    else:
                        args_dict[key] = False

            return_dict = dict(
                args=args_dict, type=self.tasks[name]["defs"][ref]["title"]
            )

            return return_dict


def launch_task_in_thread(task_manager, task_name):
    """
    Launch a fractal task subprocess.

    This function is designed to be called with @thread_worker decorator
    to run tasks in a separate thread without blocking the napari GUI.

    Args:
        task_manager: FractalTaskManager instance
        task_name: Name of the task to execute

    Yields:
    # Is it necessary/wanted?
        str: Progress messages during execution

    Returns:
        str: The task name upon completion

    Raises:
        subprocess.CalledProcessError: If the subprocess fails
    """
    print(f"[Thread] Starting task: {task_name}")
    yield f"Preparing task: {task_name}"

    path_to_executable = task_manager.get_executable_path(task_name)
    path_to_task_args = task_manager.get_path_to_json(task_name)
    pixi_manifest_path = task_manager.get_pixi_manifest_path(task_name)

    print(f"[Thread] Executable: {path_to_executable}")
    print(f"[Thread] Task args: {path_to_task_args}")

    yield "Launching subprocess..."

    task_wrapper_path = Path(__file__).with_name("task_wrapper.py")
    if pixi_manifest_path:
        command = [
            "pixi",
            "run",
            "--manifest-path",
            pixi_manifest_path,
            "python",
            str(task_wrapper_path),
            "--executable",
            path_to_executable,
            "--path_to_task_args",
            path_to_task_args,
        ]
    else:
        print(
            "[Thread] No pixi manifest path found; falling back to current interpreter."
        )
        command = [
            sys.executable,
            str(task_wrapper_path),
            "--executable",
            path_to_executable,
            "--path_to_task_args",
            path_to_task_args,
        ]

    # Run the subprocess with output capture inside the package's pixi env
    p = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    yield "Task running..."

    # Wait for process to complete
    stdout, stderr = p.communicate()

    # Check if subprocess failed
    if p.returncode != 0:
        error_msg = f"Subprocess failed with return code {p.returncode}"
        if stderr:
            error_msg += f"\n\nSTDERR:\n{stderr}"
        if stdout:
            error_msg += f"\n\nSTDOUT:\n{stdout}"
        print(f"[Thread] ERROR: {error_msg}")
        raise subprocess.CalledProcessError(
            p.returncode, p.args, stdout, stderr
        )

    # Print output for debugging
    if stdout:
        print(f"[Thread] STDOUT:\n{stdout}")
    if stderr:
        print(f"[Thread] STDERR:\n{stderr}")

    print(f"[Thread] Task completed successfully: {task_name}")
    yield "Task completed!"
    return task_name



class TasksQWidget(QWidget):
    def __init__(self, napari_viewer):
        super().__init__()
        self._viewer = napari_viewer

        self.exec_btn_dict = dict()

        ### Dictionary of TaskManager
        self.task_manager = FractalTaskManager()

        ### Add PackageImporter
        self.package_importer = PackageImporter()

        ### Core widget components
        self.main_container = QWidget()
        self.tab_container = QTabWidget()

        ### Container to select napari layer
        image_input_container = QWidget()
        image_input_container.setLayout(QHBoxLayout())
        image_input_label = QLabel("Input:")
        image_input_label.setFont(QFont("Arial", 14, weight=QFont.Bold))
        image_input_container.layout().addWidget(image_input_label)
        self._image_layers = QComboBox(self)
        image_input_container.layout().addWidget(self._image_layers)
        image_input_container.layout().setSpacing(0)

        ### Select workflow with tasks
        self.workflow_adder_container = QWidget()
        self.workflow_adder_container.setLayout(QVBoxLayout())

        self.workflow_adder_btn = QPushButton("Add task package")
        self.workflow_adder_btn.clicked.connect(self._select_workflow_file)
        self.workflow_adder_container.layout().addWidget(
            self.workflow_adder_btn
        )

        select_workflow_container = QWidget()
        select_workflow_container.setLayout(QHBoxLayout())
        workflow_label = QLabel("Select task:")
        workflow_label.setFont(QFont("Arial", 14, weight=QFont.Bold))
        select_workflow_container.layout().addWidget(workflow_label)

        self.workflow_combo_box = QComboBox(self)
        select_workflow_container.layout().addWidget(self.workflow_combo_box)

        self.workflow_adder_container.layout().addWidget(
            select_workflow_container
        )

        ### Container to add more tabs with tasks
        task_adder_container = QWidget()
        task_adder_container.setLayout(QHBoxLayout())

        self.task_adder_btn = QPushButton("Add task")
        self.task_adder_btn.clicked.connect(self._add_task)
        task_adder_container.layout().addWidget(self.task_adder_btn)

        ### Beautification...
        icon_img_container = QWidget()
        icon_img_container.setLayout(QHBoxLayout())
        im_path = abspath(__file__, "logo_images/fractal_logo.png")
        icon_img = QPixmap(im_path)
        icon_size_inner = QSize(120, 120)
        icon_size_outer = QSize(130, 130)
        icon_img = icon_img.scaled(
            icon_size_inner,
            Qt.KeepAspectRatio,
            transformMode=Qt.SmoothTransformation,
        )
        icon_label = QLabel()
        icon_label.setPixmap(icon_img)
        icon_label.setFixedSize(
            icon_size_outer.width(), icon_size_outer.height()
        )
        icon_img_container.layout().addWidget(icon_label)

        main_title = QLabel("Fractal Task Launcher")
        main_title.setFont(QFont("Arial", 16, weight=QFont.Bold))
        ### Main container
        self.main_container.setLayout(QVBoxLayout())
        self.main_container.setFixedHeight(500)
        self.main_container.layout().addWidget(main_title)
        self.main_container.layout().addWidget(icon_img_container)
        self.main_container.layout().addWidget(image_input_container)
        self.main_container.layout().addWidget(self.workflow_adder_container)
        self.main_container.layout().addWidget(task_adder_container)

        ### Tasks container
        self.tab_container.addTab(self.main_container, "Main")

        self.setLayout(QHBoxLayout())
        self.layout().addWidget(self.tab_container)

        self._update_combo_boxes()

    def _update_combo_boxes(self):
        for layer_name in [
            self._image_layers.itemText(i)
            for i in range(self._image_layers.count())
        ]:
            layer_name_index = self._image_layers.findText(layer_name)
            self._image_layers.removeItem(layer_name_index)

        for layer in [
            l
            for l in self._viewer.layers
            if isinstance(l, napari.layers.Image)
        ]:
            if layer.name not in [
                self._image_layers.itemText(i)
                for i in range(self._image_layers.count())
            ]:
                self._image_layers.addItem(layer.name)

    def _select_workflow_file(self):
        # Open workflow package with PackageImporter

        path_to_package = QFileDialog().getOpenFileName(
            self, "Select workflow package", ".", "workflow specs (*.tar.gz)"
        )[0]

        self.package_importer.open_package(path_to_package)

        path_to_workflow = self.package_importer.loaded_packages[
            self.package_importer.package_name
        ]["manifest_path"]
        package_meta = self.package_importer.loaded_packages[
            self.package_importer.package_name
        ]

        print(os.path.split(path_to_workflow)[0])

        workflow_args = self._get_json_params(path_to_workflow)

        for task in workflow_args["task_list"]:
            # if task.get("category") in INCLUDE_CATEGORIES:
            is_parallel = True if task["type"] == "parallel" else False

            if is_parallel:
                self.workflow_combo_box.addItem(task["name"])
                self.task_manager.add_task(
                    name=task["name"],
                    package_name=self.package_importer.package_name,
                    parent_dir=os.path.split(path_to_workflow)[0],
                    executable_parallel=task["executable_parallel"],
                    properties=task["args_schema_parallel"]["properties"],
                    defs=task["args_schema_parallel"].get("$defs", None),
                    required=task["args_schema_parallel"]["required"],
                    type=task["args_schema_parallel"]["type"],
                    title=task["args_schema_parallel"]["title"],
                    pixi_manifest_path=package_meta["pixi_manifest_path"],
                )

    def _fetch_subprocess_output(self, task_name):
        print(f"Received task_name={task_name}")
        if task_name in ["Thresholding Label Task", "Cellpose Segmentation"]:
            wipe_cache()
            # Remove and reload zarr
            props = self.task_manager.get_properties(task_name)
            try:
                path_to_zarr = props["zarr_url"]["value"]
            except TypeError as e:
                print("Error fetching zarr_url from task properties:", e)
                self._update_execute_buttons(is_enabled=True)
                return

            # Maybe we can allow the user to select this from a drop-down menu of all possible fields?
            out_layer_name = None
            if task_name == "Thresholding Label Task":
                out_layer_name = props["label_name"]["value"]
            elif task_name == "Cellpose Segmentation":
                out_layer_name = props["output_label_name"]["value"]

            if not out_layer_name:
                print("No output layer name defined; skipping layer reload.")
                self._update_execute_buttons(is_enabled=True)
                return

            print(f"out_layer_name={out_layer_name}")

            for layer in self._viewer.layers:
                if isinstance(layer, napari.layers.Labels):
                    self._viewer.layers.remove(layer.name)

            zarr_layer_data = napari_get_reader(path_to_zarr)()
            for layer_data in zarr_layer_data:
                if layer_data[-1] == "labels":
                    layer = napari.layers.Layer.create(*layer_data)
                    print(out_layer_name, layer.name)
                    if layer.name == out_layer_name:
                        layer.visible = True
                        self._viewer.add_layer(layer)

        # Re-enable execute buttons after task completes
        self._update_execute_buttons(is_enabled=True)

    def _update_execute_buttons(self, is_enabled=True):
        for name in self.exec_btn_dict.keys():
            self.exec_btn_dict[name].setEnabled(is_enabled)

    def _execute_task(self, task_name):
        selected_layer = self._viewer.layers[self._image_layers.currentText()]

        # Get zarr path from layer
        if hasattr(selected_layer, "source") and hasattr(
            selected_layer.source, "path"
        ):
            path_to_zarr = selected_layer.source.path
        elif (
            hasattr(selected_layer, "metadata")
            and "path" in selected_layer.metadata
        ):
            path_to_zarr = selected_layer.metadata["path"]
        else:
            path_to_zarr = None

        self.task_manager.update_task_property(
            task_name, "zarr_url", path_to_zarr
        )

        # TODO: This should be set from Zarr metadata file
        self.task_manager.update_task_property(
            task_name, "channel", self._image_layers.currentText()
        )

        task_properties = self.task_manager.get_properties(task_name)
        for property in [
            k for k in task_properties.keys() if k not in IGNORE_PROPERTIES
        ]:
            value = self.task_manager.get_widget_value(task_name, property)
            self.task_manager.update_task_property(task_name, property, value)

        self.task_manager.write_to_json(task_name)

        # Launch subprocess in separate thread to avoid GUI freezing
        # Using napari's @thread_worker decorator for clean threading
        self._update_execute_buttons(is_enabled=False)

        # Create a threaded worker using the @thread_worker decorator
        worker = thread_worker(launch_task_in_thread)(
            self.task_manager, task_name
        )

        # Connect progress updates (from yield statements)
        worker.yielded.connect(lambda msg: print(f"[Progress] {msg}"))

        # Connect the finished signal to handle task completion
        worker.finished.connect(self._fetch_subprocess_output)

        # Connect error handling with task context
        worker.errored.connect(
            lambda error_info, name=task_name: self._handle_task_error(
                name, error_info
            )
        )

        # Start the worker
        worker.start()

    def _handle_task_error(self, task_name, error_info):
        """Handle errors that occur during task execution."""
        exc_type, exc_value, exc_traceback = error_info
        print(f"ERROR during task '{task_name}': {exc_value}")
        import traceback

        traceback.print_exception(exc_type, exc_value, exc_traceback)

        if isinstance(exc_value, subprocess.CalledProcessError):
            if exc_value.stdout:
                print("[Thread] Subprocess STDOUT:\n" + exc_value.stdout)
            if exc_value.stderr:
                print("[Thread] Subprocess STDERR:\n" + exc_value.stderr)

        try:
            napari.utils.notifications.show_error(
                f"Task '{task_name}' failed: {exc_value}"
            )
        except Exception:  # noqa: BLE001
            # Notifications are best-effort; continue cleanup even if they fail.
            pass

        # Re-enable buttons so the user can attempt another run
        self._update_execute_buttons(is_enabled=True)
        # self.thread = QThread(parent=self)
        # self.worker = TaskWorker()
        # self.worker.task_name = task_name
        # self.worker.manifest_path = self.package_importer.loaded_packages[package_name]['pixi_manifest_path']
        # self.worker.task_manager = self.task_manager
        # self.worker.moveToThread(self.thread)
        #
        # self.thread.started.connect(self.worker.run)
        # self.worker.finished.connect(self._fetch_subprocess_output)
        #
        # self.thread.start()
        # QLabel describing state of progress
        # self.thread.finished.connect(
        #     lambda: self.stepLabel.setText("Long-Running Step: 0")
        # )
        #

    def _task_tab_exists(self, task_name):
        for child_widget in self.tab_container.findChildren(QWidget):
            if isinstance(child_widget, QWidget):
                if child_widget.objectName() == task_name:
                    return True
        return False

    def _add_task(self):
        task_name = self.workflow_combo_box.currentText()

        if self._task_tab_exists(task_name):
            _widget = self.tab_container.findChild(QWidget, task_name)
            self.tab_container.setCurrentWidget(_widget)
        else:
            self._add_task_tab(task_name)

    def _add_task_tab(self, task_name):
        task_container = QTabWidget(objectName=f"{task_name}")
        main_container = QWidget(objectName=f"{task_name}_main")
        main_container.setLayout(QVBoxLayout())

        task_properties = self.task_manager.get_properties(task_name)

        # TODO: Function to build an individual parameter widget should be separated out to enable recursive addition
        widget_dict = dict()
        # Automatically read zarr and enum options
        for prop_key in task_properties.keys():
            object_name = f"{task_name}+{prop_key}"

            with_default_value = True
            try:
                default_value = task_properties[prop_key]["default"]
            except KeyError:
                with_default_value = False

            if (
                "type" in task_properties[prop_key].keys()
                and prop_key not in IGNORE_PROPERTIES
            ):
                if task_properties[prop_key]["type"] in [
                    "integer",
                    "float",
                    "number",
                    "string",
                ]:
                    widget_dict[prop_key] = QLineEdit(objectName=object_name)
                    if with_default_value:
                        widget_dict[prop_key].setText(str(default_value))

                elif task_properties[prop_key]["type"] == "boolean":
                    widget_dict[prop_key] = QCheckBox(objectName=object_name)
                    if with_default_value:
                        if default_value:
                            widget_dict[prop_key].setChecked(True)
                        else:
                            widget_dict[prop_key].setChecked(False)

                elif task_properties[prop_key]["type"] == "object":
                    pass

            elif (
                "$ref" in task_properties[prop_key].keys()
                and prop_key not in IGNORE_PROPERTIES
            ):
                defs = self.task_manager.get_defs(task_name)
                ref = os.path.split(task_properties[prop_key]["$ref"])[-1]
                defs_props = defs[ref]["properties"]

                widget_dict_ = dict()
                for def_prop_key in defs_props.keys():
                    object_name_ = object_name + f"+{def_prop_key}"

                    with_default_value = True
                    try:
                        default_value = defs_props[def_prop_key]["default"]
                    except KeyError:
                        with_default_value = False

                    if "type" in defs_props[def_prop_key].keys():
                        if defs_props[def_prop_key]["type"] in [
                            "integer",
                            "float",
                            "number",
                            "string",
                        ]:
                            widget_dict_[def_prop_key] = QLineEdit(
                                objectName=object_name_
                            )
                            if with_default_value:
                                widget_dict_[def_prop_key].setText(
                                    str(default_value)
                                )

                        elif defs_props[def_prop_key]["type"] == "boolean":
                            widget_dict_[def_prop_key] = QCheckBox(
                                objectName=object_name_
                            )
                            if with_default_value:
                                if default_value:
                                    widget_dict_[def_prop_key].setChecked(True)
                                else:
                                    widget_dict_[def_prop_key].setChecked(
                                        False
                                    )

                widget_dict[prop_key] = widget_dict_

            elif prop_key not in IGNORE_PROPERTIES:
                widget_dict[prop_key] = QLineEdit(objectName=object_name)
                if with_default_value:
                    widget_dict[prop_key].setText(str(default_value))

        for prop_key in widget_dict:
            if isinstance(widget_dict[prop_key], dict):
                defs = self.task_manager.get_defs(task_name)
                ref = os.path.split(task_properties[prop_key]["$ref"])[-1]
                defs_props = defs[ref]["properties"]

                outer_container = QWidget()
                outer_container.setLayout(QVBoxLayout())
                for prop_key_ in widget_dict[prop_key].keys():
                    container = QWidget()
                    container.setLayout(QHBoxLayout())
                    qlabel_ = QLabel(defs_props[prop_key_]["title"])
                    try:
                        qlabel_.setToolTip(
                            defs_props[prop_key_]["description"]
                        )
                        qlabel_.setToolTipDuration(3000)
                    except KeyError:
                        pass
                    container.layout().addWidget(qlabel_)

                    container.layout().addWidget(
                        widget_dict[prop_key][prop_key_]
                    )
                    outer_container.layout().addWidget(container)

                task_container.addTab(
                    outer_container, task_properties[prop_key]["title"]
                )

            else:
                container = QWidget()
                container.setLayout(QHBoxLayout())
                qlabel_ = QLabel(task_properties[prop_key]["title"])
                try:
                    qlabel_.setToolTip(
                        task_properties[prop_key]["description"]
                    )
                    qlabel_.setToolTipDuration(3000)
                except KeyError:
                    pass
                container.layout().addWidget(qlabel_)

                container.layout().addWidget(widget_dict[prop_key])
                main_container.layout().addWidget(container)

        self.task_manager.add_widget_dict(task_name, widget_dict)

        self.exec_btn_dict[task_name] = QPushButton("Execute task")
        self.exec_btn_dict[task_name].clicked.connect(
            lambda: self._execute_task(task_name)
        )
        main_container.layout().addWidget(self.exec_btn_dict[task_name])

        task_close_button = QPushButton("Remove task")
        task_close_button.clicked.connect(lambda: self._close_tab(task_name))
        main_container.layout().addWidget(task_close_button)

        task_container.addTab(main_container, "Main")

        self.tab_container.addTab(task_container, task_name)

    def _close_tab(self, task_name):
        # TODO: Explicitly handle task_manager dictionaries
        self.task_manager.remove_widget_dict(task_name)

        for child_widget in self.tab_container.findChildren(QWidget):
            if isinstance(child_widget, QWidget):
                if task_name in child_widget.objectName():
                    child_widget.deleteLater()

        self.tab_container.removeTab(self.tab_container.currentIndex())

    def _get_json_params(self, path_to_json):
        with open(path_to_json) as f:
            return json.load(f)
