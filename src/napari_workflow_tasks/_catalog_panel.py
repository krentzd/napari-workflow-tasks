"""
Qt panel to manage task packages: remove installed ones, install from the
Fractal catalog (every known Fractal task, with a per-package version
picker) or from a source archive (.tar.gz) on this computer; shown in its
own window (``CatalogDialog``).

Packages come from the official Fractal task-list sources; versions are
listed newest-first and the task list shown is the one of the selected
version (read from that version's ``__FRACTAL_MANIFEST__.json``).
"""

from __future__ import annotations

import logging
import os
import tarfile
import time

from napari.qt.threading import create_worker
from napari.utils.notifications import show_error, show_info, show_warning
from qtpy.QtCore import QObject, Qt, QTimer, Signal
from qtpy.QtGui import QBrush, QColor, QFont
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from workflow_tasks_core import (
    Catalog,
    Package,
    install,
    manifests,
    pixi_config,
    snapshot,
)
from workflow_tasks_core.manifests import VersionInfo, task_kind
from workflow_tasks_core.progress import format_elapsed, install_step
from workflow_tasks_core.runner import RUNNABLE_KINDS
from workflow_tasks_core.task_packages import TaskPackage

logger = logging.getLogger(__name__)

ROLE = Qt.UserRole


PIXI_MISSING = (
    "<b>pixi was not found.</b> Task packages can't be installed or run "
    "without it. Install pixi from <a href='https://pixi.sh'>pixi.sh</a>, "
    "then restart napari.")


def pixi_warning() -> QLabel | None:
    """A banner saying that pixi is missing, or None when pixi is found."""
    try:
        pixi_config.find_pixi()
    except FileNotFoundError:
        pass
    else:
        return None
    label = QLabel(PIXI_MISSING)
    label.setWordWrap(True)
    label.setOpenExternalLinks(True)
    label.setTextInteractionFlags(Qt.TextBrowserInteraction)
    label.setStyleSheet("QLabel { border: 1px solid #e0a800; "
                        "border-radius: 4px; padding: 6px; }")
    return label


class _LogEmitter(QObject):
    line = Signal(str)


def _comma_list(text: str) -> list[str]:
    """``"a, b,"`` -> ``["a", "b"]``."""
    return [item.strip() for item in text.split(",") if item.strip()]


def _labeled_row(label: str, edit: QLineEdit) -> QWidget:
    row = QWidget()
    row.setLayout(QHBoxLayout())
    row.layout().setContentsMargins(0, 0, 0, 0)
    row.layout().addWidget(QLabel(label))
    row.layout().addWidget(edit)
    return row


PINS_TOOLTIP = (
    "Extra version constraints for this task's environment, comma "
    "separated. Pinned versions always win. Re-install to apply."
)
SKIP_TOOLTIP = (
    "Dependencies to leave out of this task's environment, comma "
    "separated, e.g. packages that cannot be installed on this computer. "
    "Tasks that import them fail when they are run. Not used for the "
    "install from a release's pixi.lock. Re-install to apply."
)
PINS_LOCK_TOOLTIP = (
    "This release ships a pixi.lock: it is used as is if it satisfies the "
    "pins, otherwise the package's pixi config is re-solved with them "
    "(or its wheel is installed with them)."
)


class CatalogPanel(QWidget):
    """
    The installed packages (to remove them), and two ways to install a task
    package: the Fractal task catalog, and a source archive on this
    computer ("Install from file"); with the progress below them.

    Emits ``installed(package, task_name)`` once a package version has been
    installed (``package``: a ``workflow_tasks_core.Package``;
    ``task_name``: the task the user selected in the catalog, or ""), and
    ``removed(package)`` once one has been removed.
    """

    installed = Signal(object, str)
    removed = Signal(object)

    COLUMNS = ["Task", "Version", "Type"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._packages: dict[str, TaskPackage] = {}
        self._infos: dict[tuple[str, str], VersionInfo] = {}
        self._items: dict[str, QTreeWidgetItem] = {}
        self._combos: dict[str, QComboBox] = {}
        self._workers = []  # keep references alive
        # Loading the catalog
        self._n_errors = 0
        self._rate_limit_msg = None
        self._snapshot_at = None
        # The selected version: available extras (tooltip)
        self._extras_tip = ""
        # "<package>==<version>" being installed
        self._install_label = ""
        self._package_job = False  # an install or a removal is running
        self._removal_allowed = True  # False while a task runs

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.pixi_warning = pixi_warning()
        if self.pixi_warning is not None:
            outer.addWidget(self.pixi_warning)

        # Installed packages: remove the ones not needed any more
        installed_box = QGroupBox("Installed packages")
        installed_layout = QVBoxLayout(installed_box)
        installed_layout.setContentsMargins(15, 15, 15, 15)
        self.installed_list = QListWidget()
        self.installed_list.setMaximumHeight(140)
        self.installed_list.itemSelectionChanged.connect(
            self._update_remove_button)
        installed_layout.addWidget(self.installed_list)
        self.remove_btn = QPushButton("Remove package")
        self.remove_btn.clicked.connect(self._remove_selected)
        remove_row = QHBoxLayout()
        remove_row.addStretch(1)
        remove_row.addWidget(self.remove_btn)
        installed_layout.addLayout(remove_row)
        outer.addWidget(installed_box)

        catalog_box = QGroupBox("Fractal task catalog")
        layout = QVBoxLayout(catalog_box)
        layout.setContentsMargins(15, 15, 15, 15)
        outer.addWidget(catalog_box)

        # Search + refresh
        top = QWidget()
        top.setLayout(QHBoxLayout())
        top.layout().setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search tasks, packages, categories…")
        self.search.textChanged.connect(self._apply_filter)
        self.refresh_btn = QPushButton("Load catalog")
        self.refresh_btn.clicked.connect(self.refresh)
        top.layout().addWidget(self.search)
        top.layout().addWidget(self.refresh_btn)
        layout.addWidget(top)

        # Tree: package rows (with version combo) -> task rows
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLUMNS))
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setMinimumHeight(320)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.resizeSection(1, 120)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(2, 95)
        self.tree.currentItemChanged.connect(self._on_selection_changed)
        self.tree.itemDoubleClicked.connect(lambda *_: self._install_selected())
        layout.addWidget(self.tree)

        # Extras
        self.extras_edit = QLineEdit()
        self.extras_edit.setPlaceholderText("e.g. fractal-tasks (comma separated)")
        layout.addWidget(_labeled_row("pip extras:", self.extras_edit))

        # Pinned dependencies (like fractal-server's pinned_package_versions)
        self.pins_edit = QLineEdit()
        self.pins_edit.setPlaceholderText("e.g. cellpose<4.2, numpy==1.26.4")
        self.pins_edit.setToolTip(PINS_TOOLTIP)
        layout.addWidget(_labeled_row("Pinned packages:", self.pins_edit))

        # Dependencies to leave out (e.g. CUDA-only packages on macOS)
        self.skip_edit = QLineEdit()
        self.skip_edit.setPlaceholderText("e.g. cupy-cuda12x")
        self.skip_edit.setToolTip(SKIP_TOOLTIP)
        layout.addWidget(_labeled_row("Skip packages:", self.skip_edit))

        self.lock_check = QCheckBox("Use the package's pixi.lock when available")
        self.lock_check.setChecked(True)
        self.lock_check.setToolTip(
            "Install the exact dependency versions the task authors locked and "
            "tested (like fractal-server's pixi task collection). "
            "pip extras are not used for that install. Pinned packages win: "
            "the lock is used only if it satisfies them, otherwise the "
            "package's pixi config is re-solved with the pins. "
            "Falls back to resolving dependencies (the package's pixi config, "
            "then its wheel, with your extras and pins) if the release has no "
            "pixi.lock, or it does not cover this platform or the pins, fails "
            "to install or its tasks can't be imported."
        )
        self.lock_check.toggled.connect(self._update_lock_hint)
        layout.addWidget(self.lock_check)

        # Install
        self.install_btn = QPushButton("Install package")
        self.install_btn.setEnabled(False)
        self.install_btn.clicked.connect(self._install_selected)
        layout.addWidget(self.install_btn)

        # Install from a source archive on this computer
        file_box = QGroupBox("Install from file")
        file_layout = QVBoxLayout(file_box)
        file_layout.setContentsMargins(15, 15, 15, 15)
        self.file_path = QLineEdit()
        self.file_path.setPlaceholderText(
            "Source archive (.tar.gz) of a task package")
        self.file_path.textChanged.connect(self._update_file_button)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_file)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path:"))
        path_row.addWidget(self.file_path, 1)
        path_row.addWidget(browse_btn)
        file_layout.addLayout(path_row)
        self.file_btn = QPushButton("Install from file")
        self.file_btn.setToolTip(
            "Install the task package in this source archive (e.g. made "
            "with `python -m build --sdist`), with the pip extras, pinned "
            "and skipped packages and pixi.lock choice of the catalog above")
        self.file_btn.clicked.connect(self._install_from_file)
        file_layout.addWidget(self.file_btn)
        outer.addWidget(file_box)

        # Busy indicator for slow steps (loading, installing, caching)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        # Latest install output line, under the step
        self.detail = QLabel("")
        self.detail.setStyleSheet("color: gray;")
        self.detail.setWordWrap(True)
        self.detail.setVisible(False)
        outer.addWidget(self.detail)
        self._update_file_button()
        self.refresh_installed()

        self._step = ""
        self._busy_since = None
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(1000)
        self._busy_timer.timeout.connect(self._show_step)

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    def _start_busy(self, step: str):
        self._busy_since = time.monotonic()
        self.progress.setVisible(True)
        self.detail.setText("")
        self._set_step(step)
        self._busy_timer.start()

    def _set_step(self, step: str):
        self._step = step
        self._show_step()

    def _show_step(self):
        if self._busy_since is None:
            return
        elapsed = format_elapsed(time.monotonic() - self._busy_since)
        self.status.setText(f"{self._step}… ({elapsed})")

    def _stop_busy(self):
        self._busy_timer.stop()
        self._busy_since = None
        self.progress.setVisible(False)
        self.detail.setVisible(False)

    # ------------------------------------------------------------------
    # Loading the catalog
    # ------------------------------------------------------------------

    def refresh(self):
        self.tree.clear()
        self._packages.clear()
        self._items.clear()
        self._combos.clear()
        self._infos.clear()
        self.refresh_btn.setEnabled(False)
        self._start_busy("Fetching task packages")
        self._n_errors = 0
        self._rate_limit_msg = None
        self._snapshot_at = None

        # The daily catalog snapshot (if it can't be read: PyPI/GitHub live)
        worker = create_worker(
            snapshot.iter_catalog,
            _ignore_errors=True,  # handled by _on_worker_error
        )
        worker.yielded.connect(self._on_catalog_item)
        worker.finished.connect(self._on_catalog_done)
        worker.errored.connect(self._on_worker_error)
        self._start(worker)

    def _on_catalog_item(self, item):
        kind, payload = item
        if kind == "snapshot":
            self._snapshot_at = payload
            return
        if kind == "error" and payload[0] == "catalog snapshot":
            # Not fatal: iter_catalog falls back to querying live
            logger.warning(f"Catalog snapshot {payload[1]}")
            self._set_step("Catalog snapshot unavailable, querying live")
            return
        if kind == "error":
            source, msg = payload
            self._n_errors += 1
            if "rate limit" in msg:
                self._rate_limit_msg = msg.split(": ", 1)[-1]
            logger.warning(f"Could not load {source}: {msg}")
            return
        pkg, info = payload
        if not pkg.versions:
            return
        self._packages[pkg.pkg_name] = pkg
        if info is not None:
            self._infos[(pkg.pkg_name, pkg.default_version.version)] = info
        self._add_package_row(pkg)
        self._set_step(f"Loaded {len(self._packages)} packages")

    def _on_catalog_done(self):
        self._stop_busy()
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("Refresh")
        n_tasks = sum(
            self._items[p].childCount() for p in self._items
        )
        msg = f"{len(self._packages)} packages, {n_tasks} tasks"
        msg += (
            f" (catalog updated {self._snapshot_at[:10]})."
            if self._snapshot_at else " (live)."
        )
        if self._n_errors:
            msg += f" {self._n_errors} source(s) failed to load"
            msg += (
                f": {self._rate_limit_msg}" if self._rate_limit_msg
                else " (see log)."
            )
            show_warning(msg)
        self.status.setText(msg)
        self._apply_filter()

    # ------------------------------------------------------------------
    # Tree rows
    # ------------------------------------------------------------------

    def _add_package_row(self, pkg: TaskPackage):
        item = QTreeWidgetItem([pkg.pkg_name, "", pkg.kind])
        font = QFont()
        font.setBold(True)
        item.setFont(0, font)
        item.setData(0, ROLE, ("package", pkg.pkg_name, None))
        item.setToolTip(0, f"{pkg.pkg_name}\n{pkg.source}")

        # Insert alphabetically
        names = sorted([*self._items, pkg.pkg_name], key=str.lower)
        self.tree.insertTopLevelItem(names.index(pkg.pkg_name), item)
        self._items[pkg.pkg_name] = item

        combo = QComboBox()
        for v in pkg.versions:
            label = v.version
            if v.pixi_lock:
                label += "  🔒"
            if install.is_installed(pkg.pkg_name, v.version):
                label += "  ✓"
            if v.yanked:
                label += "  (yanked)"
            elif v.is_prerelease:
                label += "  (pre-release)"
            combo.addItem(label, v.version)
        combo.setToolTip(
            "Choose the package version (newest first).\n"
            "🔒 = ships a pixi.lock (exact, tested dependencies)\n✓ = installed"
        )
        combo.setCurrentIndex(pkg.versions.index(pkg.default_version))
        combo.currentIndexChanged.connect(
            lambda _i, name=pkg.pkg_name: self._on_version_changed(name)
        )
        self.tree.setItemWidget(item, 1, combo)
        self._combos[pkg.pkg_name] = combo

        self._fill_tasks(pkg.pkg_name, pkg.default_version.version)

    def selected_version(self, pkg_name: str) -> str:
        return self._combos[pkg_name].currentData()

    def _fill_tasks(self, pkg_name: str, version: str):
        item = self._items[pkg_name]
        item.takeChildren()
        info = self._infos.get((pkg_name, version))
        if info is None:
            loading = QTreeWidgetItem(["Loading manifest…"])
            loading.setFlags(Qt.NoItemFlags)
            item.addChild(loading)
            return

        gray = QBrush(QColor(140, 140, 140))
        for task in info.task_list:
            # Snapshot summaries carry the kind; full manifests are classified
            ttype = task.get("kind") or task_kind(task)
            child = QTreeWidgetItem(
                [task["name"], "", ttype.replace("converter_", "converter ")]
            )
            child.setData(2, ROLE, ttype)
            child.setData(0, ROLE, ("task", pkg_name, task["name"]))
            meta = " · ".join(
                x for x in (task.get("category"), task.get("modality")) if x
            )
            tooltip = (meta + "\n\n" if meta else "") + (task.get("docs_info") or "")
            if ttype not in RUNNABLE_KINDS:
                for col in range(len(self.COLUMNS)):
                    child.setForeground(col, gray)
                reason = (
                    "Converters create new OME-Zarrs from raw data, so they "
                    "don't run on images loaded in the viewer."
                    if ttype.startswith("converter") else
                    f"{ttype} tasks can't be launched from napari."
                )
                tooltip = f"{reason}\n\n" + tooltip
            child.setToolTip(0, tooltip[:1500])
            item.addChild(child)
        item.setText(0, f"{pkg_name} ({len(info.task_list)})")
        self._apply_filter()

    def _on_version_changed(self, pkg_name: str):
        version = self.selected_version(pkg_name)
        self._fill_tasks(pkg_name, version)
        self._update_extras_suggestion()
        if (pkg_name, version) in self._infos:
            return

        pkg = self._packages[pkg_name]
        worker = create_worker(
            manifests.get_display_info, pkg, version, _ignore_errors=True
        )
        worker.returned.connect(
            lambda info, p=pkg_name, v=version: self._on_info_loaded(p, v, info)
        )
        worker.errored.connect(self._on_worker_error)
        self._start(worker)

    def _on_info_loaded(self, pkg_name, version, info):
        self._infos[(pkg_name, version)] = info
        # Ignore stale results if the user picked another version meanwhile
        if self.selected_version(pkg_name) == version:
            self._fill_tasks(pkg_name, version)
            self._update_extras_suggestion()

    # ------------------------------------------------------------------
    # Filtering / selection
    # ------------------------------------------------------------------

    def _apply_filter(self):
        query = self.search.text().strip().lower()
        for pkg_name, item in self._items.items():
            pkg_match = query in pkg_name.lower()
            any_child = False
            info = self._infos.get((pkg_name, self.selected_version(pkg_name)))
            tasks = {t["name"]: t for t in info.task_list} if info else {}
            for i in range(item.childCount()):
                child = item.child(i)
                task = tasks.get(child.text(0), {})
                haystack = " ".join(
                    [
                        child.text(0),
                        task.get("category") or "",
                        task.get("modality") or "",
                        " ".join(task.get("tags") or []),
                    ]
                ).lower()
                visible = not query or pkg_match or query in haystack
                child.setHidden(not visible)
                any_child = any_child or visible
            item.setHidden(bool(query) and not (pkg_match or any_child))
            item.setExpanded(bool(query) and any_child)

    def _selected(self):
        item = self.tree.currentItem()
        if item is None:
            return None
        data = item.data(0, ROLE)
        if not data:
            return None
        kind, pkg_name, task_name = data
        return kind, pkg_name, task_name, item

    def _on_selection_changed(self, *_):
        sel = self._selected()
        if sel is None:
            self.install_btn.setEnabled(False)
            return
        kind, pkg_name, _task, item = sel
        runnable = kind == "package" or item.data(2, ROLE) in RUNNABLE_KINDS
        self.install_btn.setEnabled(runnable)
        self._update_extras_suggestion()

    def _update_extras_suggestion(self):
        sel = self._selected()
        if sel is None:
            return
        pkg_name = sel[1]
        info = self._infos.get((pkg_name, self.selected_version(pkg_name)))
        suggested = ""
        if info and "fractal-tasks" in info.extras:
            # e.g. fractal-tasks-core<2 needs [fractal-tasks] to run its tasks
            suggested = "fractal-tasks"
        self.extras_edit.setText(suggested)
        self.pins_edit.setText(
            ", ".join(install.SUGGESTED_PINS.get(pkg_name, []))
        )
        self.skip_edit.setText(", ".join(install.suggested_skip(pkg_name)))
        self._extras_tip = (
            "Available extras: " + (", ".join(info.extras) or "none")
            if info else ""
        )
        self._update_lock_hint()

    def _update_lock_hint(self, *_):
        """Grey out extras when they won't be used (locked release). Pins
        are always used: they decide whether the lock can be used."""
        sel = self._selected()
        locked = False
        if sel is not None and sel[1] in self._packages:
            pkg = self._packages[sel[1]]
            try:
                locked = bool(pkg.get_version(self.selected_version(sel[1])).pixi_lock)
            except KeyError:
                locked = False
        use = locked and self.lock_check.isChecked()
        self.extras_edit.setEnabled(not use)
        note = (
            "Not used for the install from this release's pixi.lock; "
            "used only if that falls back to resolving dependencies"
            if use else ""
        )
        tip = self._extras_tip
        self.extras_edit.setToolTip("\n".join(t for t in (note, tip) if t))
        self.pins_edit.setEnabled(True)
        self.pins_edit.setToolTip(
            PINS_TOOLTIP + (" " + PINS_LOCK_TOOLTIP if use else "")
        )

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def _install_selected(self):
        sel = self._selected()
        if sel is None or not self.install_btn.isEnabled():
            return
        kind, pkg_name, task_name, _item = sel
        version = self.selected_version(pkg_name)
        self._install(Package(self._packages[pkg_name], version),
                      task_name or "")

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Source archive of a task package", "",
            "Source archives (*.tar.gz)")
        if path:  # not cancelled
            self.file_path.setText(path)

    def _update_file_button(self, *_):
        has_path = bool(self.file_path.text().strip())
        self.file_btn.setEnabled(has_path and not self._package_job)

    def _install_from_file(self):
        path = self.file_path.text().strip()
        if not path:
            return
        try:
            package = Package.from_file(path)
        except (OSError, ValueError, tarfile.TarError) as e:
            show_error(f"Cannot install {os.path.basename(path)}: {e}")
            return
        self._install(package, "")

    def _install(self, package: Package, task_name: str):
        """Install ``package`` in the background with the options of the
        panel; ``task_name``: the task to open afterwards ("" for none)."""
        extras = _comma_list(self.extras_edit.text())
        pins = _comma_list(self.pins_edit.text())
        skip = _comma_list(self.skip_edit.text())
        use_lock = self.lock_check.isChecked()

        self._package_job = True
        self.install_btn.setEnabled(False)
        self._update_remove_button()
        self._update_file_button()
        self.refresh_btn.setEnabled(False)
        self._install_label = package.key
        self._start_busy(f"Preparing {self._install_label}")
        self.detail.setVisible(True)

        emitter = _LogEmitter()
        emitter.line.connect(self._on_install_log)

        worker = create_worker(
            package.install, log=emitter.line.emit, extras=extras, pins=pins,
            use_lock=use_lock, skip=skip, _ignore_errors=True,
        )
        worker.returned.connect(
            lambda package: self._on_installed(package, task_name))
        worker.errored.connect(self._on_worker_error)
        worker.finished.connect(self._stop_busy)
        worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        worker.finished.connect(self._on_package_job_finished)
        worker.finished.connect(self._on_selection_changed)
        worker._emitter = emitter  # keep alive
        self._start(worker)

    def _on_package_job_finished(self):
        self._package_job = False
        self._update_file_button()
        self._update_remove_button()

    def _on_install_log(self, line: str):
        logger.info(line)
        if not line.strip():
            return
        step = install_step(line)
        if step:
            self._set_step(f"{step}: {self._install_label}")
        self.detail.setText(line.strip()[:200])

    def _on_installed(self, package, task_name):
        self._stop_busy()
        entry = package.entry
        combo = self._combos.get(package.name)
        if combo is not None:
            idx = combo.findData(package.version)
            if idx >= 0 and "✓" not in combo.itemText(idx):
                combo.setItemText(idx, combo.itemText(idx) + "  ✓")
        how = " from its pixi.lock" if entry.get("pixi_frozen") else ""
        pins = entry.get("pins_applied")
        if pins:
            how += f" (pinned: {', '.join(pins['pins'])})"
        skipped = entry.get("skipped")
        if skipped:
            how += f" (skipped: {', '.join(skipped)})"
        if not entry.get("pixi_frozen") and entry.get("lock_note"):
            how += f" (not from its pixi.lock: {entry['lock_note']})"
        self.status.setText(f"Installed {package.key}{how}.")
        show_info(f"Installed {package.key}{how}")
        self.refresh_installed()
        self.installed.emit(package, task_name)

    # ------------------------------------------------------------------
    # Installed packages
    # ------------------------------------------------------------------

    def refresh_installed(self):
        """List the package versions installed on this computer."""
        self.installed_list.clear()
        for package in Catalog.installed():
            item = QListWidgetItem(str(package))
            item.setData(ROLE, package)
            self.installed_list.addItem(item)
        if not self.installed_list.count():
            item = QListWidgetItem("(no packages installed yet)")
            item.setFlags(Qt.NoItemFlags)
            self.installed_list.addItem(item)
        self._update_remove_button()

    def set_removal_allowed(self, allowed: bool):
        """Packages can't be removed while a task runs (it may use one)."""
        self._removal_allowed = allowed
        self._update_remove_button()

    def _selected_installed(self):
        item = self.installed_list.currentItem()
        if item is None or not item.isSelected():
            return None
        return item.data(ROLE)

    def _update_remove_button(self):
        self.remove_btn.setEnabled(
            self._selected_installed() is not None
            and self._removal_allowed and not self._package_job)
        self.remove_btn.setToolTip(
            "Delete the environment of the selected package version"
            if self._removal_allowed else
            "Packages can't be removed while a task runs")

    def _remove_selected(self):
        package = self._selected_installed()
        if package is None or not self.remove_btn.isEnabled():
            return
        answer = QMessageBox.question(
            self, "Remove package",
            f"Remove {package}? Its environment is deleted; open tabs of "
            "its tasks are closed.")
        if answer != QMessageBox.Yes:
            return
        self._package_job = True
        self._update_remove_button()
        self.install_btn.setEnabled(False)
        self._update_file_button()
        self._start_busy(f"Removing {package}")
        worker = create_worker(package.remove, _ignore_errors=True)
        worker.returned.connect(lambda _: self._on_removed(package))
        worker.errored.connect(self._on_worker_error)
        worker.finished.connect(self._stop_busy)
        worker.finished.connect(self._on_package_job_finished)
        worker.finished.connect(self._on_selection_changed)
        self._start(worker)

    def _on_removed(self, package):
        combo = self._combos.get(package.name)
        if combo is not None:
            idx = combo.findData(package.version)
            if idx >= 0:
                combo.setItemText(idx, combo.itemText(idx).replace("  ✓", ""))
        self.status.setText(f"Removed {package}.")
        show_info(f"Removed {package}")
        self.refresh_installed()
        self.removed.emit(package)

    # ------------------------------------------------------------------

    def _on_worker_error(self, exc):
        logger.error(f"{type(exc).__name__}: {exc}")
        show_error(f"Fractal task catalog: {exc}"[:1000])
        self.status.setText(f"Error: {exc}"[:400])
        self.refresh_btn.setEnabled(True)

    def _start(self, worker):
        self._workers.append(worker)
        worker.finished.connect(
            lambda w=worker: self._workers.remove(w)
            if w in self._workers
            else None
        )
        worker.start()


class CatalogDialog(QDialog):
    """Window to manage task packages: remove installed ones, install new
    ones from the Fractal catalog or from a source archive (see
    ``panel.installed`` and ``panel.removed``)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage packages")
        self.resize(640, 900)
        self.panel = CatalogPanel(self)
        layout = QVBoxLayout(self)
        layout.addWidget(self.panel)
