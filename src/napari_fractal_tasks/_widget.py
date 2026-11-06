import logging
from pathlib import Path

from qtpy.QtCore import QSize, Qt
from qtpy.QtGui import QBrush, QColor, QFont, QIcon
from qtpy.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from workflow_tasks_core import Catalog, Package
from workflow_tasks_core.runner import RUNNABLE_KINDS
from workflow_tasks_core.task_packages import version_sort_key

from ._catalog_panel import CatalogDialog, pixi_warning
from ._task_tab import TaskTab

logger = logging.getLogger(__name__)

# Tree item data: the key of a task that can be opened (None on other rows)
KEY_ROLE = Qt.UserRole


def _row(*widgets):
    row = QWidget()
    layout = QHBoxLayout(row)
    for widget in widgets:
        layout.addWidget(widget)
    return row


class TasksQWidget(QWidget):
    """The plugin's dock widget: a Main tab listing the loaded (installed)
    task packages and their tasks, and one tab per opened task, with that
    task's input and ROI table. Packages are added in a separate window
    (``CatalogDialog``)."""

    # Width napari gives the dock when it opens: room for the package list
    # and the task forms (it can still be made narrower)
    PREFERRED_WIDTH = 500

    def __init__(self, napari_viewer):
        super().__init__()
        self._viewer = napari_viewer
        self._tasks = {}  # task key -> Task, for every installed task
        self._tabs = {}   # task key -> its open TaskTab
        self._packages = {}  # package name -> {version: Package}
        self._package_items = {}  # package name -> its row in the tree
        self._version_combos = {}  # package name -> its version dropdown
        self._running = False  # a task is running (one at a time)
        self._catalog_dialog = None  # created when first opened

        # ---------------- Title & logo ----------------
        main_title = QLabel("Fractal Task Launcher")
        # A widget stylesheet, not setFont: napari's stylesheet sets the
        # font size of every QLabel, which would override setFont.
        main_title.setStyleSheet(
            "font-family: Arial; font-size: 24pt; font-weight: bold;")
        main_title.setAlignment(Qt.AlignCenter)
        logo_path = (Path(__file__).parent / 'logo_images'
                     / 'napari-fractal-tasks-logo.svg')
        logo = QLabel()
        # Rendering through QIcon draws the SVG at the target size
        # (and screen scale), so it stays sharp.
        logo.setPixmap(QIcon(str(logo_path)).pixmap(120, 120))
        logo.setFixedSize(130, 130)

        # ---------------- Loaded packages and their tasks ----------------
        packages_box = QGroupBox("Loaded packages")
        packages_layout = QVBoxLayout(packages_box)
        packages_layout.setSpacing(10)
        packages_layout.setContentsMargins(15, 15, 15, 15)

        manage_button = QPushButton("Manage packages")
        manage_button.setToolTip("Install task packages from the Fractal "
                                 "catalog or from a .tar.gz file, or remove "
                                 "installed ones")
        manage_button.clicked.connect(self.show_catalog)
        top_row = QHBoxLayout()
        top_row.addStretch(1)
        top_row.addWidget(manage_button)
        packages_layout.addLayout(top_row)

        self.pixi_warning = pixi_warning()
        if self.pixi_warning is not None:
            packages_layout.addWidget(self.pixi_warning)

        self.task_tree = QTreeWidget()
        self.task_tree.setHeaderLabels(["Task", "Version", "Type"])
        self.task_tree.setMinimumHeight(320)
        header = self.task_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.resizeSection(1, 90)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.task_tree.itemDoubleClicked.connect(
            lambda *_: self._open_selected_task())
        self.task_tree.currentItemChanged.connect(
            lambda *_: self._update_open_button())
        packages_layout.addWidget(self.task_tree)

        self.open_button = QPushButton("Add task")
        self.open_button.setToolTip("Open the selected task in its own tab "
                                    "(or double-click it)")
        self.open_button.clicked.connect(self._open_selected_task)
        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        bottom_row.addWidget(self.open_button)
        bottom_row.addStretch(1)
        packages_layout.addLayout(bottom_row)

        # ---------------- Main tab ----------------
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.addWidget(main_title)
        main_layout.addWidget(_row(logo))
        main_layout.addWidget(packages_box)
        main_layout.addStretch(1)

        self.tabs = QTabWidget()
        main_scroll = QScrollArea()
        main_scroll.setWidgetResizable(True)
        main_scroll.setWidget(main)
        self.tabs.addTab(main_scroll, "Main")
        self.setLayout(QHBoxLayout())
        self.layout().addWidget(self.tabs)

        for package in Catalog.installed():  # no network
            self.add_package(package)
        self._update_open_button()

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(max(hint.width(), self.PREFERRED_WIDTH), hint.height())

    # ------------------------------------------------------------------
    # Loaded packages
    # ------------------------------------------------------------------

    def show_catalog(self):
        """Open the window that installs and removes task packages."""
        if self._catalog_dialog is None:
            self._catalog_dialog = CatalogDialog(self)
            panel = self._catalog_dialog.panel
            panel.installed.connect(self.add_package)
            panel.removed.connect(self.remove_package)
            panel.set_removal_allowed(not self._running)
        self._catalog_dialog.show()
        self._catalog_dialog.raise_()
        self._catalog_dialog.activateWindow()

    def add_package(self, package: Package, open_task_name: str = ""):
        """List an installed package version, show its tasks and open the
        tab of ``open_task_name`` (a manifest task name)."""
        versions = self._packages.setdefault(package.name, {})
        versions[package.version] = package
        if package.name not in self._package_items:
            self._add_package_row(package.name)

        self._show_versions(package.name, package.version)

        if open_task_name:
            key = package.task(open_task_name).key
            if key in self._tasks:  # not for converters
                self.open_task(key)

    def remove_package(self, package: Package):
        """Forget a removed package version: close the tabs of its tasks and
        take it out of the list."""
        versions = self._packages.get(package.name, {})
        if versions.pop(package.version, None) is None:
            return
        for key, task in list(self._tasks.items()):
            if task.package.key == package.key:
                self.close_task(key)
                del self._tasks[key]
        if versions:
            self._show_versions(package.name, None)
            return
        del self._packages[package.name]
        del self._version_combos[package.name]
        item = self._package_items.pop(package.name)
        self.task_tree.takeTopLevelItem(self.task_tree.indexOfTopLevelItem(item))
        self._update_open_button()

    def _show_versions(self, name: str, version):
        """Fill a package's version dropdown (newest first), choose
        ``version`` (None: the newest) and show its tasks."""
        combo = self._version_combos[name]
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(sorted(self._packages[name], key=version_sort_key,
                              reverse=True))
        if version is not None:
            combo.setCurrentText(version)
        combo.blockSignals(False)
        self._show_tasks(name)

    def _add_package_row(self, name: str):
        """A package's row: its name and a dropdown of its installed
        versions (newest first); its tasks are the rows below it."""
        item = QTreeWidgetItem([name, "", ""])
        item.setToolTip(0, name)
        font = QFont()
        font.setBold(True)
        item.setFont(0, font)
        names = sorted([*self._package_items, name], key=str.lower)
        self.task_tree.insertTopLevelItem(names.index(name), item)
        self._package_items[name] = item

        combo = QComboBox()
        combo.setToolTip("Installed versions of the package (newest first)")
        combo.currentIndexChanged.connect(lambda _i: self._show_tasks(name))
        self.task_tree.setItemWidget(item, 1, combo)
        self._version_combos[name] = combo

    def _show_tasks(self, name: str):
        """List the tasks of the version chosen in a package's dropdown."""
        item = self._package_items[name]
        version = self._version_combos[name].currentText()
        package = self._packages[name][version]
        item.takeChildren()
        try:
            task_names = package.tasks
        except Exception as e:  # noqa: BLE001 - e.g. an unreadable manifest
            logger.warning(f"Cannot list the tasks of {package.key}: {e}")
            item.addChild(QTreeWidgetItem([f"(cannot read its tasks: {e})"]))
            return

        gray = QBrush(QColor(140, 140, 140))
        for task_name in task_names:
            task = package.task(task_name)
            kind = "converter" if task.kind.startswith("converter") else task.kind
            child = QTreeWidgetItem([task_name, "", kind.replace("_", " ")])
            item.addChild(child)
            if task.kind not in RUNNABLE_KINDS:
                child.setForeground(0, gray)
                child.setForeground(2, gray)
                child.setToolTip(0, "Converters create new OME-Zarrs from raw "
                                    "data, so they don't run on images "
                                    "loaded in the viewer.")
                continue
            child.setToolTip(0, task.docs_info[:1500])
            child.setData(0, KEY_ROLE, task.key)
            self._tasks[task.key] = task
        item.setExpanded(True)
        self._update_open_button()

    # ------------------------------------------------------------------
    # Task tabs
    # ------------------------------------------------------------------

    def _selected_task_key(self):
        item = self.task_tree.currentItem()
        return item.data(0, KEY_ROLE) if item is not None else None

    def _update_open_button(self):
        self.open_button.setEnabled(self._selected_task_key() is not None)

    def _open_selected_task(self):
        key = self._selected_task_key()
        if key is not None:
            self.open_task(key)

    def open_task(self, key: str):
        """Show the tab of a task (``Task.key``), opening it if needed."""
        if key not in self._tabs:
            tab = TaskTab(self._tasks[key], self._viewer)
            tab.remove_requested.connect(lambda: self.close_task(key))
            tab.run_started.connect(lambda: self._set_running(True))
            tab.run_finished.connect(lambda: self._set_running(False))
            tab.set_blocked(self._running)
            self._tabs[key] = tab
            name = self._tasks[key].name
            short = name if len(name) <= 22 else name[:21] + '…'
            index = self.tabs.addTab(tab, short)
            self.tabs.setTabToolTip(index, key)
        self.tabs.setCurrentWidget(self._tabs[key])

    def _set_running(self, running: bool):
        """Run one task at a time: tasks may write to the same OME-Zarr."""
        self._running = running
        for tab in self._tabs.values():
            tab.set_blocked(running)
        if self._catalog_dialog is not None:
            self._catalog_dialog.panel.set_removal_allowed(not running)

    def close_task(self, key: str):
        tab = self._tabs.pop(key, None)
        if tab is None:
            return
        tab.disconnect_viewer()
        self.tabs.removeTab(self.tabs.indexOf(tab))
        tab.deleteLater()
