"""
Qt panel listing every known Fractal task, with a per-package version picker.

Packages come from the official Fractal task-list sources; versions are
listed newest-first and the task list shown is the one of the selected
version (read from that version's ``__FRACTAL_MANIFEST__.json``).
"""

from __future__ import annotations

import logging
import time

from napari.qt.threading import create_worker
from napari.utils.notifications import show_error, show_info, show_warning
from qtpy.QtCore import QObject, Qt, QTimer, Signal
from qtpy.QtGui import QBrush, QColor, QFont
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from workflow_tasks_core import install, manifests, snapshot
from workflow_tasks_core.manifests import VersionInfo, task_kind
from workflow_tasks_core.progress import format_elapsed, install_step
from workflow_tasks_core.runner import RUNNABLE_KINDS
from workflow_tasks_core.task_packages import TaskPackage

logger = logging.getLogger(__name__)

ROLE = Qt.UserRole


class _LogEmitter(QObject):
    line = Signal(str)


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


class CatalogPanel(QGroupBox):
    """
    Emits ``installed(pkg, version, info, entry, task_name)`` once a package
    version has been installed; ``task_name`` is the task the user selected
    (or "" when a whole package was selected).
    """

    installed = Signal(object, str, object, dict, str)

    COLUMNS = ["Task", "Version", "Type"]

    def __init__(self, parent=None):
        super().__init__("Fractal task catalog", parent)
        self._packages: dict[str, TaskPackage] = {}
        self._infos: dict[tuple[str, str], VersionInfo] = {}
        self._items: dict[str, QTreeWidgetItem] = {}
        self._combos: dict[str, QComboBox] = {}
        self._workers = []  # keep references alive

        layout = QVBoxLayout()
        layout.setContentsMargins(15, 15, 15, 15)
        self.setLayout(layout)

        # Search + refresh
        top = QWidget()
        top.setLayout(QHBoxLayout())
        top.layout().setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search tasks, packages, categories…")
        self.search.textChanged.connect(self._apply_filter)
        self.refresh_btn = QPushButton("Load catalog")
        self.refresh_btn.clicked.connect(self.refresh)
        self.live_check = QCheckBox("Live")
        self.live_check.setToolTip(
            "Query PyPI/GitHub directly instead of the daily catalog snapshot "
            "(finds releases from today, but uses GitHub API requests)."
        )
        top.layout().addWidget(self.search)
        top.layout().addWidget(self.live_check)
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
        extras_row = QWidget()
        extras_row.setLayout(QHBoxLayout())
        extras_row.layout().setContentsMargins(0, 0, 0, 0)
        extras_row.layout().addWidget(QLabel("pip extras:"))
        self.extras_edit = QLineEdit()
        self.extras_edit.setPlaceholderText("e.g. fractal-tasks (comma separated)")
        extras_row.layout().addWidget(self.extras_edit)
        layout.addWidget(extras_row)

        # Pinned dependencies (like fractal-server's pinned_package_versions)
        pins_row = QWidget()
        pins_row.setLayout(QHBoxLayout())
        pins_row.layout().setContentsMargins(0, 0, 0, 0)
        pins_row.layout().addWidget(QLabel("Pinned packages:"))
        self.pins_edit = QLineEdit()
        self.pins_edit.setPlaceholderText("e.g. cellpose<4.2, numpy==1.26.4")
        self.pins_edit.setToolTip(PINS_TOOLTIP)
        pins_row.layout().addWidget(self.pins_edit)
        layout.addWidget(pins_row)

        # Dependencies to leave out (e.g. CUDA-only packages on macOS)
        skip_row = QWidget()
        skip_row.setLayout(QHBoxLayout())
        skip_row.layout().setContentsMargins(0, 0, 0, 0)
        skip_row.layout().addWidget(QLabel("Skip packages:"))
        self.skip_edit = QLineEdit()
        self.skip_edit.setPlaceholderText("e.g. cupy-cuda12x")
        self.skip_edit.setToolTip(SKIP_TOOLTIP)
        skip_row.layout().addWidget(self.skip_edit)
        layout.addWidget(skip_row)

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

        # Busy indicator for slow steps (loading, installing, caching)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status = QLabel("Click “Load catalog” to list all Fractal tasks.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # Latest install output line, under the step
        self.detail = QLabel("")
        self.detail.setStyleSheet("color: gray;")
        self.detail.setWordWrap(True)
        self.detail.setVisible(False)
        layout.addWidget(self.detail)

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

        worker = create_worker(
            snapshot.iter_catalog, live=self.live_check.isChecked(),
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
        tip = getattr(self, "_extras_tip", "")
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
        pkg = self._packages[pkg_name]
        version = self.selected_version(pkg_name)
        extras = [
            e.strip() for e in self.extras_edit.text().split(",") if e.strip()
        ]
        pins = [
            p.strip() for p in self.pins_edit.text().split(",") if p.strip()
        ]
        skip = [
            s.strip() for s in self.skip_edit.text().split(",") if s.strip()
        ]
        use_lock = self.lock_check.isChecked()

        self.install_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self._install_label = f"{pkg_name}=={version}"
        self._start_busy(f"Preparing {self._install_label}")
        self.detail.setVisible(True)

        emitter = _LogEmitter()
        emitter.line.connect(self._on_install_log)

        def _install():
            entry = install.install_version(
                pkg, version, extras=extras, log=emitter.line.emit, pins=pins,
                use_lock=use_lock, skip=skip,
            )
            emitter.line.emit("Reading the task list")
            return manifests.get_version_info(pkg, version), entry

        worker = create_worker(_install, _ignore_errors=True)
        worker.returned.connect(
            lambda res: self._on_installed(
                pkg, version, res[0], res[1], task_name or ""
            )
        )
        worker.errored.connect(self._on_worker_error)
        worker.finished.connect(self._stop_busy)
        worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        worker.finished.connect(self._on_selection_changed)
        worker._emitter = emitter  # keep alive
        self._start(worker)

    def _on_install_log(self, line: str):
        logger.info(line)
        if not line.strip():
            return
        step = install_step(line)
        if line.startswith("Reading the task list"):
            step = "Reading the task list"
        if step:
            self._set_step(f"{step}: {self._install_label}")
        self.detail.setText(line.strip()[:200])

    def _on_installed(self, pkg, version, info, entry, task_name):
        self._stop_busy()
        combo = self._combos.get(pkg.pkg_name)
        if combo is not None:
            idx = combo.findData(version)
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
        self.status.setText(f"Installed {pkg.pkg_name}=={version}{how}.")
        show_info(f"Installed {pkg.pkg_name}=={version}{how}")
        self.installed.emit(pkg, version, info, entry, task_name)

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
