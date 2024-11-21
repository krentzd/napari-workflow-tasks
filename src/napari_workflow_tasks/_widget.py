
from typing import TYPE_CHECKING
from qtpy.QtWidgets import (QHBoxLayout, QPushButton, QWidget, QTabWidget,
                            QTableWidget, QVBoxLayout, QAbstractItemView, QLabel,
                            QLineEdit, QTabBar, QFileDialog, QCheckBox, QComboBox,
                            QScrollArea)
from qtpy.QtGui import QPixmap, QFont
from qtpy.QtCore import Qt, QSize
import json
import subprocess
import os
import napari

import dask.array as da

from ome_zarr.reader import Reader
from ome_zarr.io import parse_url
from ome_zarr.types import LayerData

from napari_ome_zarr._reader import napari_get_reader

from pathlib import Path
# from .ome_zarr_task_manager import OMEZarrTaskManager

if TYPE_CHECKING:
    import napari

IGNORE_PROPERTIES = ['zarr_url', 'channel', 'channels_to_include', 'channels_to_exclude', 'measure_texture']

def abspath(root, relpath):
    root = Path(root)
    if root.is_dir():
        path = root/relpath
    else:
        path = root.parent/relpath
    return str(path.absolute())

class OMEZarrTaskManager:
    # Manage tasks by keeping track of what each tab contains and what executable it links to
    def __init__(self):
        self.tasks = dict()

    def add_task(self,
                 name,
                 parent_dir,
                 executable_parallel,
                 properties,
                 required,
                 type,
                 title):

        task_dict = dict(
            title=title,
            parent_dir=parent_dir,
            executable_parallel=executable_parallel,
            properties=properties,
            required=required,
            type=type,
            widget_dict=dict()
        )
        self.tasks[name] = task_dict

    def get_executable_path(self,
                            name):
        parent_dir = self.tasks[name]['parent_dir']
        exec_fname = self.tasks[name]['executable_parallel']

        return os.path.join(parent_dir, exec_fname)

    def get_task(self,
                 name):
        return self.tasks[name]

    def get_properties(self,
                       name):
        return self.tasks[name]['properties']

    def write_to_json(self,
                      name):

        parent_dir = self.tasks[name]['parent_dir']
        title = self.tasks[name]['title']
        path_to_json = os.path.join(parent_dir, f'{title}.json')

        # if self.tasks[name]['properties']['label_name']['value'] == "":
        #     self.tasks[name]['properties']['label_name']['value'] = f"{self.tasks[name]['properties']['channel']['value']}_{self.tasks[name]['title']}"

        args_dict = dict()
        for prop_key in self.tasks[name]['properties'].keys():
            if 'value' in self.tasks[name]['properties'][prop_key]:
                args_dict[prop_key] = self.tasks[name]['properties'][prop_key]['value']

        with open(path_to_json, 'w') as f:
            json.dump(args_dict, f)

    def get_title(self,
                  name):
        return self.tasks[name]['title']

    def update_task_property(self,
                             name,
                             property,
                             value):

        print('Property dict updated', name, property, value)
        try:
            self.tasks[name]['properties'][property]['value'] = value
        except KeyError:
            print(f'Property {property} not defined in MANIFEST')

    def add_widget_dict(self,
                        name,
                        widget_dict):
        self.tasks[name]['widget_dict'] = widget_dict

    def get_widget_value(self,
                         name,
                         property):
        widget = self.tasks[name]['widget_dict'][property]

        if isinstance(widget, QLineEdit):
            value = widget.text()
            type = self.tasks[name]['properties'][property]['type']

            if type == 'integer':
                return int(value)
            elif type == 'float':
                return float(value)
            else:
                return value

        elif isinstance(widget, QCheckBox):
            if widget.isChecked():
                return True
            else:
                return False

class TasksQWidget(QWidget):
    def __init__(self, napari_viewer):
        super().__init__()
        self._viewer = napari_viewer

        self.exec_dict = dict()
        ### Dictionary of TaskManager
        self.task_manager = OMEZarrTaskManager()

        ### Core widget components
        self.main_container = QWidget()
        self.tab_container = QTabWidget()

        ### Container to select napari layer
        image_input_container = QWidget()
        image_input_container.setLayout(QHBoxLayout())
        image_input_label = QLabel('Channel:')
        image_input_label.setFont(QFont('Arial', 14, weight=QFont.Bold))
        image_input_container.layout().addWidget(image_input_label)
        self._image_layers = QComboBox(self)
        image_input_container.layout().addWidget(self._image_layers)
        image_input_container.layout().setSpacing(0)

        ### Select workflow with tasks
        self.workflow_adder_container = QWidget()
        self.workflow_adder_container.setLayout(QVBoxLayout())

        self.workflow_adder_btn = QPushButton("Add workflow")
        self.workflow_adder_btn.clicked.connect(self._select_workflow_file)
        self.workflow_adder_container.layout().addWidget(self.workflow_adder_btn)

        select_workflow_container = QWidget()
        select_workflow_container.setLayout(QHBoxLayout())
        workflow_label = QLabel("Select workflow:")
        workflow_label.setFont(QFont('Arial', 14, weight=QFont.Bold))
        select_workflow_container.layout().addWidget(workflow_label)

        self.workflow_combo_box = QComboBox(self)
        select_workflow_container.layout().addWidget(self.workflow_combo_box)

        self.workflow_adder_container.layout().addWidget(select_workflow_container)

        ### Container to add more tabs with tasks
        task_adder_container = QWidget()
        task_adder_container.setLayout(QHBoxLayout())

        self.task_adder_btn = QPushButton("Add task")
        self.task_adder_btn.clicked.connect(self._add_task_tab)
        task_adder_container.layout().addWidget(self.task_adder_btn)

        ### Beautification...
        icon_img_container = QWidget()
        icon_img_container.setLayout(QHBoxLayout())
        im_path = abspath(__file__, f'logo_images/fractal_logo.png')
        icon_img = QPixmap(im_path)
        icon_size_inner = QSize(120, 120)
        icon_size_outer = QSize(130, 130)
        icon_img = icon_img.scaled(icon_size_inner, Qt.KeepAspectRatio, transformMode=Qt.SmoothTransformation)
        icon_label = QLabel()
        icon_label.setPixmap(icon_img)
        icon_label.setFixedSize(icon_size_outer.width(), icon_size_outer.height())
        icon_img_container.layout().addWidget(icon_label)

        main_title = QLabel('Interactive OME-Zarr Fractal Task Launcher')
        main_title.setFont(QFont('Arial', 16, weight=QFont.Bold))
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
        for layer_name in [self._image_layers.itemText(i) for i in range(self._image_layers.count())]:
            layer_name_index = self._image_layers.findText(layer_name)
            self._image_layers.removeItem(layer_name_index)

        for layer in [l for l in self._viewer.layers if isinstance(l, napari.layers.Image)]:
            if layer.name not in [self._image_layers.itemText(i) for i in range(self._image_layers.count())]:
                self._image_layers.addItem(layer.name)

    def _select_workflow_file(self):
        path_to_workflow = QFileDialog().getOpenFileName(self, "Select workflow file", ".",
                                                         "workflow specs (*.json)")[0]

        workflow_args = self._get_json_params(path_to_workflow)

        for task in workflow_args["task_list"]:
            self.workflow_combo_box.addItem(task["name"])
            self.task_manager.add_task(name=task["name"],
                                       parent_dir=os.path.split(path_to_workflow)[0],
                                       executable_parallel=task["executable_parallel"],
                                       properties=task["args_schema_parallel"]["properties"],
                                       required=task["args_schema_parallel"]["required"],
                                       type=task["args_schema_parallel"]["type"],
                                       title=task["args_schema_parallel"]["title"])

    def _execute_task(self, task_name):

        selected_layer = self._viewer.layers[self._image_layers.currentText()]
        path_to_zarr = selected_layer.source.path
        self.task_manager.update_task_property(task_name, 'zarr_url', path_to_zarr)
        self.task_manager.update_task_property(task_name, 'channel', self._image_layers.currentText())

        task_properties = self.task_manager.get_properties(task_name)
        for property in [k for k in task_properties.keys() if k not in IGNORE_PROPERTIES]:
        # for property in ['threshold', 'label_name', 'min_size', 'overwrite']:
            value = self.task_manager.get_widget_value(task_name, property)
            self.task_manager.update_task_property(task_name, property, value)

        # Since the output is written to the Zarr file, remove the layer containing the zarr and reload it again once the process is finished

        self.task_manager.write_to_json(task_name)

        path_to_executable = self.task_manager.get_executable_path(task_name)
        print(path_to_executable)

        p = subprocess.Popen(['python', path_to_executable])
        p.wait()

        # Remove and reload zarr
        # Future MANIFESTS should include output type
        if task_name == 'Thresholding Label Task':
            props = self.task_manager.get_properties(task_name)
            out_layer_name = props['label_name']['value']



            for layer in self._viewer.layers:
                if isinstance(layer, napari.layers.Labels):
                    self._viewer.layers.remove(layer.name)


            zarr_layer_data = napari_get_reader(os.path.join(path_to_zarr))()
            for layer_data in zarr_layer_data:
                if layer_data[-1] == 'labels':
                    layer = napari.layers.Layer.create(*layer_data)
                    print(out_layer_name, layer.name)
                    if layer.name == out_layer_name:
                        layer.visible = True
                        self._viewer.add_layer(layer)

    def _add_task_tab(self):

        task_name = self.workflow_combo_box.currentText()

        task_container = QWidget(objectName=f'{task_name}')
        task_container.setLayout(QVBoxLayout())

        task_properties = self.task_manager.get_properties(task_name)

        widget_dict = dict()
        for prop_key in task_properties.keys():
            if 'type' in task_properties[prop_key].keys() and prop_key not in IGNORE_PROPERTIES:
                object_name = f'{task_name}+{prop_key}'

                if task_properties[prop_key]['type'] == "integer":
                    widget_dict[prop_key] = QLineEdit(objectName=object_name)

                elif task_properties[prop_key]['type'] == "string":
                    widget_dict[prop_key] = QLineEdit(objectName=object_name)

                elif task_properties[prop_key]['type'] == "boolean":
                    widget_dict[prop_key] = QCheckBox(objectName=object_name)

                elif task_properties[prop_key]['type'] == "float":
                    widget_dict[prop_key] = QLineEdit(objectName=object_name)

        for prop_key in widget_dict.keys():
            container = QWidget()
            container.setLayout(QHBoxLayout())
            container.layout().addWidget(QLabel(task_properties[prop_key]['title']))

            container.layout().addWidget(widget_dict[prop_key])
            task_container.layout().addWidget(container)

        self.task_manager.add_widget_dict(task_name, widget_dict)

        # scroll_area = QScrollArea()
        # scroll_area.setWidgetResizable(True)
        # scroll_area.setWidget(task_container)

        task_execute_button = QPushButton("Execute task")
        task_execute_button.clicked.connect(lambda: self._execute_task(task_name))
        task_container.layout().addWidget(task_execute_button)

        task_close_button = QPushButton("Remove task")
        task_close_button.clicked.connect(self._close_tab)
        task_container.layout().addWidget(task_close_button)

        # scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        # scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # scroll_area.setWidget(task_container)
        #
        # task_container.setCentralWidget(scroll_area)

        self.tab_container.addTab(task_container, task_name)

    def _close_tab(self):
        # Need to handle OMETaskManager entries
        self.tab_container.removeTab(self.tab_container.currentIndex())

    def _get_json_params(self, path_to_json):
        with open(path_to_json) as f:
            return json.load(f)
