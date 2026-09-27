import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from PyQt6.QtCore import (
    QAbstractTableModel,
    QElapsedTimer,
    QModelIndex,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QActionGroup, QBrush, QColor, QIcon, QKeySequence, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import covers
import expergo as core
from models import (
    ART_COL,
    FIELDS,
    HEADERS,
    LRC_COL,
    FolderLayout,
    Track,
    apply_cover,
    auto_cover,
    fetch_lrc,
    find_flacs,
    flac_name,
    move_track,
    plan_reorganize,
    remove_empty_dirs,
    save_track,
    write_tags,
)
DIRTY_BRUSH = QBrush(QColor(255, 190, 0, 70))
MISSING_BRUSH = QBrush(QColor(220, 60, 60))
EDIT_TRIGGERS = (
    QAbstractItemView.EditTrigger.DoubleClicked
    | QAbstractItemView.EditTrigger.EditKeyPressed
    | QAbstractItemView.EditTrigger.AnyKeyPressed
)

os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
REQUIRED_TOOLS = ("ffmpeg", "flac", "ffmpeg-normalize")


def tools_ok(parent) -> bool:
    missing = [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]
    if missing:
        QMessageBox.critical(parent, "Missing tools", f"Not found on PATH: {', '.join(missing)}\nSee README.md.")
    return not missing


def std_icon(theme: str, fallback: QStyle.StandardPixmap) -> QIcon:
    return QIcon.fromTheme(theme, QApplication.style().standardIcon(fallback))


class Cancelled(Exception):
    pass


class Worker(QThread):
    """Runs fn(item) for every item on a thread pool, reporting log lines and progress."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    current = pyqtSignal(str)

    def __init__(self, items, fn, workers: int = 1, parent=None):
        super().__init__(parent)
        self.items, self.fn, self.workers = list(items), fn, max(1, workers)
        self.results = []
        self._stop = False

    def stop(self):
        self._stop = True

    def _run_one(self, item):
        if self._stop:
            raise Cancelled()
        name = getattr(item, "name", None)
        if name:
            self.current.emit(name)
        return self.fn(item)

    def run(self):
        total, done = len(self.items), 0
        self.progress.emit(0, total)
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self._run_one, item): item for item in self.items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                    self.results.append((item, result))
                    if isinstance(result, str) and result.strip():
                        self.log.emit(result.strip())
                except Cancelled:
                    pass
                except subprocess.CalledProcessError as e:
                    self.log.emit(f"ERROR {item.name}: {e}\n{(e.stderr or '')[-800:]}")
                except Exception as e:
                    self.log.emit(f"ERROR {getattr(item, 'name', item)}: {e}")
                done += 1
                self.progress.emit(done, total)
        if self._stop:
            self.log.emit("Stopped.")


# Background threads not parented to a widget, kept alive until they finish so closing a dialog can't
# destroy a QThread that is still blocked on the network.
_live_threads: set[QThread] = set()


class Task(QThread):
    progress = pyqtSignal(object)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(self))
        except Exception as e:
            self.failed.emit(str(e))

    def start(self):
        _live_threads.add(self)
        self.finished.connect(lambda: _live_threads.discard(self))
        super().start()


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


class ProgressStrip(QFrame):
    def __init__(self, stop_action: QAction | None = None):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        self.bar = QProgressBar()
        self.bar.setMinimumWidth(200)
        self.detail = QLabel()
        self.detail.setStyleSheet("color: palette(mid);")
        # Ignored width so long filenames get clipped instead of stretching the window
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.clock = QLabel()
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._done = self._total = 0

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(self.title)
        row.addWidget(self.bar, 2)
        row.addWidget(self.detail, 3)
        row.addWidget(self.clock)
        if stop_action:
            stop = QToolButton()
            stop.setDefaultAction(stop_action)
            stop.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            row.addWidget(stop)
        self.hide()

    def start(self, title: str, total: int = 0):
        self.title.setText(title)
        self.detail.clear()
        self._elapsed.start()
        self._timer.start()
        self.set_progress(0, total)
        self.show()

    def set_title(self, title: str):
        self.title.setText(title)

    def set_progress(self, done: int, total: int):
        self._done, self._total = done, total
        if total == 0 or (total == 1 and done == 0):
            self.bar.setRange(0, 0)  # unknown / single long step: show a busy animation instead of 0%
        else:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self.bar.setFormat(f"{done} / {total}  (%p%)")
        self._tick()

    def set_detail(self, text: str):
        self.detail.setText(text)
        self.detail.setToolTip(text)

    def _tick(self):
        seconds = self._elapsed.elapsed() / 1000 if self._elapsed.isValid() else 0
        text = format_duration(seconds)
        if 0 < self._done < self._total:
            text += f"  ·  ~{format_duration(seconds / self._done * (self._total - self._done))} left"
        self.clock.setText(text)

    def finish(self):
        self._timer.stop()
        self.hide()


class TrackModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.tracks: list[Track] = []

    def set_tracks(self, tracks):
        self.beginResetModel()
        self.tracks = list(tracks)
        self.endResetModel()

    def add_tracks(self, tracks):
        if not tracks:
            return
        start = len(self.tracks)
        self.beginInsertRows(QModelIndex(), start, start + len(tracks) - 1)
        self.tracks += tracks
        self.endInsertRows()

    def remove_rows(self, rows):
        for row in sorted(rows, reverse=True):
            self.beginRemoveRows(QModelIndex(), row, row)
            del self.tracks[row]
            self.endRemoveRows()

    def refresh(self):
        if self.tracks:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.tracks) - 1, ART_COL))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        track = self.tracks[index.row()]
        col = index.column()
        if col == LRC_COL:
            if role == Qt.ItemDataRole.DisplayRole:
                return track.lrc
            if role == Qt.ItemDataRole.ForegroundRole and track.lrc != "OK":
                return MISSING_BRUSH
            if role == Qt.ItemDataRole.ToolTipRole:
                return str(track.path.with_suffix(".lrc"))
            return None
        if col == ART_COL:
            if role == Qt.ItemDataRole.DisplayRole:
                return "OK" if track.art else "Missing"
            if role == Qt.ItemDataRole.ForegroundRole and not track.art:
                return MISSING_BRUSH
            return None

        key = FIELDS[col][0]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return track.tags[key]
        if role == Qt.ItemDataRole.BackgroundRole and track.is_dirty(key):
            return DIRTY_BRUSH
        if role == Qt.ItemDataRole.ToolTipRole:
            if track.is_dirty(key):
                return f"Original: {track.orig[key]}"
            return str(track.path) if key == "FILE" else None
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.column() < LRC_COL:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or index.column() >= LRC_COL:
            return False
        self.tracks[index.row()].tags[FIELDS[index.column()][0]] = str(value)
        self.dataChanged.emit(index, index)
        return True


class FilterProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.missing_lrc = False
        self.missing_art = False
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_filter(self, text=None, missing_lrc=None, missing_art=None):
        if text is not None:
            self.text = text.lower()
        if missing_lrc is not None:
            self.missing_lrc = missing_lrc
        if missing_art is not None:
            self.missing_art = missing_art
        self.invalidate()

    def filterAcceptsRow(self, row, parent):
        track = self.sourceModel().tracks[row]
        if self.missing_lrc and track.lrc == "OK":
            return False
        if self.missing_art and track.art:
            return False
        return not self.text or any(self.text in v.lower() for v in track.tags.values())


class TrackTable(QTableView):
    def __init__(self, source: TrackModel):
        super().__init__()
        self.source = source
        self.proxy = FilterProxy()
        self.proxy.setSourceModel(source)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(EDIT_TRIGGERS)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.extra_actions: list[QAction] = []

    def fit_columns(self):
        self.resizeColumnsToContents()
        for col in range(len(HEADERS)):
            self.setColumnWidth(col, min(self.columnWidth(col), 350))

    def selected_rows(self) -> list[int]:
        return sorted({self.proxy.mapToSource(i).row() for i in self.selectionModel().selectedRows()})

    def selected_tracks(self) -> list[Track]:
        """Selected rows, falling back to the current row."""
        rows = self.selected_rows()
        if not rows and self.currentIndex().isValid():
            rows = [self.proxy.mapToSource(self.currentIndex()).row()]
        return [self.source.tracks[r] for r in rows]

    def current_track(self) -> Track | None:
        index = self.currentIndex()
        if not index.isValid():
            rows = self.selected_rows()
            return self.source.tracks[rows[0]] if rows else None
        return self.source.tracks[self.proxy.mapToSource(index).row()]

    def target_tracks(self) -> list[Track]:
        rows = self.selected_rows() or [
            self.proxy.mapToSource(self.proxy.index(r, 0)).row() for r in range(self.proxy.rowCount())
        ]
        return [self.source.tracks[r] for r in rows]

    def _context_menu(self, pos):
        index = self.indexAt(pos)
        rows = self.selected_rows()
        if not index.isValid() or not rows:
            return
        col = index.column()
        menu = QMenu(self)
        if col < LRC_COL and not (col == 0 and len(rows) > 1):
            menu.addAction(f"Set {HEADERS[col]} for {len(rows)} row(s)…").triggered.connect(
                lambda: self._bulk_edit(rows, col, index.data())
            )
        menu.addAction("Revert changes").triggered.connect(lambda: self._revert(rows))
        if self.extra_actions:
            menu.addSeparator()
            menu.addActions(self.extra_actions)
        menu.exec(self.viewport().mapToGlobal(pos))

    def _bulk_edit(self, rows, col, current):
        value, ok = QInputDialog.getText(self, "Bulk edit", f"{HEADERS[col]}:", text=current or "")
        if ok:
            for row in rows:
                self.source.tracks[row].tags[FIELDS[col][0]] = value
            self.source.refresh()

    def _revert(self, rows):
        for row in rows:
            track = self.source.tracks[row]
            track.tags = dict(track.orig)
        self.source.refresh()


class ImportDialog(QDialog):
    imported = pyqtSignal()

    def __init__(self, dest: Path | None, workers: int, nolrc: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Songs")
        self.resize(1000, 600)
        self.setAcceptDrops(True)
        self.worker = None
        self._copy_lock = threading.Lock()

        self.model = TrackModel()
        self.table = TrackTable(self.model)

        add_files = QPushButton("Add Files…")
        add_files.clicked.connect(self._pick_files)
        add_folder = QPushButton("Add Folder…")
        add_folder.clicked.connect(self._pick_folder)
        remove = QPushButton("Remove Selected")
        remove.clicked.connect(lambda: self.model.remove_rows(self.table.selected_rows()))
        top = QHBoxLayout()
        for w in (add_files, add_folder, remove):
            top.addWidget(w)
        top.addStretch()
        top.addWidget(QLabel("Tip: drag & drop FLACs or folders here"))

        self.dest = QLineEdit(str(dest) if dest else "")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_dest)
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Destination:"))
        dest_row.addWidget(self.dest, 1)
        dest_row.addWidget(browse)

        self.apply_rules = QCheckBox("Apply Echo rules")
        self.apply_rules.setChecked(True)
        self.apply_rules.setToolTip("Rename, resample, fix blocksize, normalize loudness and resize art after copying.\n"
                                    "Untick to copy the files as-is (only your tag edits are written).")
        self.apply_rules.toggled.connect(self._update_import_label)
        self.organize = QCheckBox("Organize into Artist / Album folders")
        self.organize.setToolTip("Uses Album Artist when set (keeps compilations together), otherwise Artist")
        self.nolrc = QCheckBox("Skip LRC download")
        self.nolrc.setChecked(nolrc)
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.setValue(workers)
        opts = QHBoxLayout()
        opts.addWidget(self.apply_rules)
        opts.addWidget(self.organize)
        opts.addWidget(self.nolrc)
        opts.addStretch()
        opts.addWidget(QLabel("Workers:"))
        opts.addWidget(self.workers)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(5000)
        self.stop_action = QAction("Stop", self)
        self.stop_action.setIcon(std_icon("process-stop", QStyle.StandardPixmap.SP_BrowserStop))
        self.stop_action.triggered.connect(lambda: self.worker and self.worker.stop())
        self.progress = ProgressStrip(self.stop_action)
        self.import_btn = QPushButton("Import && Apply Rules")
        self.import_btn.clicked.connect(self._import)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(self.progress, 1)
        bottom.addStretch()
        bottom.addWidget(self.import_btn)
        bottom.addWidget(close)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.log)
        splitter.setSizes([400, 150])
        self._update_import_label()

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addLayout(dest_row)
        layout.addLayout(opts)
        layout.addLayout(bottom)

    def _update_import_label(self):
        self.import_btn.setText("Import && Apply Rules" if self.apply_rules.isChecked() else "Import")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.add_paths([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add FLAC files", "", "FLAC (*.flac)")
        self.add_paths(files)

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add folder of FLACs")
        if folder:
            self.add_paths([folder])

    def _pick_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "Destination folder", self.dest.text())
        if folder:
            self.dest.setText(folder)

    def add_paths(self, paths):
        known = {t.path for t in self.model.tracks}
        new = []
        for path in find_flacs(paths):
            if path in known:
                continue
            try:
                new.append(Track.load(path))
            except Exception as e:
                self.log.appendPlainText(f"ERROR reading {path.name}: {e}")
        self.model.add_tracks(new)
        self.table.fit_columns()

    def _import_one(self, track: Track, dest_root: Path, layout: FolderLayout | None,
                    apply_rules: bool, nolrc: bool) -> str:
        with self._copy_lock:
            dest_dir = layout.folder_for(track.tags) if layout else dest_root
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / (flac_name(track.tags["FILE"]) if track.tags["FILE"].strip() else track.name)
            if target.exists():
                raise FileExistsError(f"{target} already exists")
            target.touch()
        shutil.copy2(track.path, target)
        lrc = track.path.with_suffix(".lrc")
        if lrc.exists():
            shutil.copy2(lrc, target.with_suffix(".lrc"))
        write_tags(target, track)
        if apply_rules:
            return f"Copied to {dest_dir}" + core.process_file(target.resolve(), nolrc)

        lines = [f"Copied {target.name} to {dest_dir}"]
        title, artist = track.tags["TITLE"].strip(), track.tags["ARTIST"].strip()
        if not nolrc and not target.with_suffix(".lrc").exists() and title and artist:
            found = core.download_lrc(target.with_suffix(".lrc"), title, artist)
            lines.append(f"  {'Fetched' if found else 'No'} LRC for {title} - {artist}")
        return "\n".join(lines)

    def _import(self):
        if not self.model.tracks:
            return
        if not self.dest.text().strip():
            QMessageBox.warning(self, "Add Songs", "Choose a destination folder first.")
            return
        apply_rules, nolrc = self.apply_rules.isChecked(), self.nolrc.isChecked()
        if apply_rules and not tools_ok(self):
            return
        core._claimed_names.clear()
        self.import_btn.setEnabled(False)
        dest_root = Path(self.dest.text().strip()).expanduser().resolve()
        layout = FolderLayout(dest_root) if self.organize.isChecked() else None
        fn = lambda t: self._import_one(t, dest_root, layout, apply_rules, nolrc)
        self.worker = Worker(self.model.tracks, fn, self.workers.value(), self)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self.progress.set_progress)
        self.worker.current.connect(self.progress.set_detail)
        self.worker.finished.connect(self._done)
        self.progress.start("Importing", len(self.worker.items))
        self.worker.start()

    def _done(self):
        if not self.worker:
            return
        done = {id(track) for track, _ in self.worker.results}
        self.model.remove_rows([i for i, t in enumerate(self.model.tracks) if id(t) in done])
        failed = len(self.model.tracks)
        self.log.appendPlainText(f"Imported {len(done)} file(s).")
        if failed:
            self.log.appendPlainText(f"{failed} failed; their copies may remain in the library (see log).")
        self.worker = None
        self.progress.finish()
        self.import_btn.setEnabled(True)
        self.imported.emit()

    def closeEvent(self, event):
        if self.worker:
            QMessageBox.information(self, "Add Songs", "Import is still running.")
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self):
        self.close()


class ReorganizeDialog(QDialog):
    """Preview of moving every track to root/Artist/Album/. Nothing is touched until the user accepts."""

    def __init__(self, tracks: list[Track], root: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reorganize Folders")
        self.resize(1000, 600)
        self.tracks, self.root = tracks, root
        self.moves: list[SimpleNamespace] = []

        intro = QLabel(f"Move every FLAC (and its .lrc) in <b>{root}</b> into <b>Artist / Album</b> folders. "
                       "Filenames are kept. Uses the saved tags on disk.")
        intro.setWordWrap(True)
        self.album_artist = QCheckBox("Use Album Artist when set")
        self.album_artist.setChecked(True)
        self.album_artist.setToolTip("Keeps compilations / features in one folder. Untick to always use the track Artist.")
        self.album_artist.toggled.connect(self._refresh)
        self.cleanup = QCheckBox("Remove empty folders afterwards")
        self.cleanup.setChecked(True)
        opts = QHBoxLayout()
        opts.addWidget(self.album_artist)
        opts.addWidget(self.cleanup)
        opts.addStretch()

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["From", "To"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)

        self.summary = QLabel()
        self.ok_btn = QPushButton()
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.ok_btn)
        bottom.addWidget(cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(opts)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)
        self._refresh()

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def _refresh(self):
        plan = plan_reorganize(self.tracks, self.root, self.album_artist.isChecked())
        self.moves = [m for m in plan if not m.conflict]
        self.table.setRowCount(len(plan))
        for row, move in enumerate(plan):
            src = QTableWidgetItem(self._rel(move.src))
            dst = QTableWidgetItem(self._rel(move.dst) + ("   (conflict: skipped)" if move.conflict else ""))
            src.setToolTip(str(move.src))
            dst.setToolTip(str(move.dst))
            if move.conflict:
                dst.setForeground(MISSING_BRUSH)
            self.table.setItem(row, 0, src)
            self.table.setItem(row, 1, dst)
        conflicts = len(plan) - len(self.moves)
        in_place = len(self.tracks) - len(plan)
        text = f"{len(self.moves)} to move · {in_place} already in place"
        if conflicts:
            text += f" · {conflicts} skipped (a file with the same name is already at the destination)"
        self.summary.setText(text)
        self.ok_btn.setText(f"Move {len(self.moves)} File(s)")
        self.ok_btn.setEnabled(bool(self.moves))



def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setStyleSheet("font-weight: bold; font-size: 11px; color: palette(mid); padding: 2px 0;")
    return label


class ArtPanel(QWidget):
    def __init__(self, actions: list[QAction]):
        super().__init__()
        self._pixmap: QPixmap | None = None

        self.image = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.image.setFrameShape(QFrame.Shape.StyledPanel)
        self.image.setMinimumSize(160, 160)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.caption = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self.info = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.info.setStyleSheet("color: palette(mid);")

        buttons = QHBoxLayout()
        buttons.addStretch()
        for act in actions:
            btn = QToolButton()
            btn.setDefaultAction(act)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            buttons.addWidget(btn)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(section_label("Album Art"))
        layout.addWidget(self.image, 1)
        layout.addWidget(self.caption)
        layout.addWidget(self.info)
        layout.addLayout(buttons)
        self.show_track(None)

    def show_track(self, track: Track | None, selected: int = 0):
        self._pixmap = None
        self.info.clear()
        if track is None:
            self.caption.setText("No track selected")
        else:
            self.caption.setText(track.tags["ALBUM"] or track.tags["TITLE"] or track.name)
            try:
                pic = covers.read_cover(track.path)
            except Exception as e:
                pic = None
                self.info.setText(f"Could not read: {e}")
            if pic:
                pixmap = QPixmap()
                if pixmap.loadFromData(pic.data):
                    self._pixmap = pixmap
                    fmt = (pic.mime or "image").split("/")[-1].upper()
                    self.info.setText(f"{pixmap.width()}×{pixmap.height()} · {fmt} · {len(pic.data) // 1024} KB")
            elif not self.info.text():
                self.info.setText("No embedded album art")
            if selected > 1:
                self.info.setText(self.info.text() + f"\n{selected} tracks selected; edits apply to all")
        self._rescale()

    def _rescale(self):
        if self._pixmap:
            self.image.setPixmap(self._pixmap.scaled(
                self.image.size() - QSize(8, 8),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            self.image.setPixmap(QPixmap())
            self.image.setText("No art")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()


class CoverDialog(QDialog):
    THUMB = 180

    def __init__(self, album: str, artist: str, count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Album Art · {count} track(s)")
        self.resize(900, 640)
        self.source: tuple[str, str] | None = None
        self._task: Task | None = None

        self.album = QLineEdit(album, placeholderText="Album")
        self.artist = QLineEdit(artist, placeholderText="Artist (optional)")
        for edit in (self.album, self.artist):
            edit.returnPressed.connect(self.search)
        search = QPushButton("Search")
        search.clicked.connect(self.search)
        top = QHBoxLayout()
        top.addWidget(self.album, 3)
        top.addWidget(self.artist, 2)
        top.addWidget(search)

        self.results = QListWidget()
        self.results.setViewMode(QListView.ViewMode.IconMode)
        self.results.setIconSize(QSize(self.THUMB, self.THUMB))
        self.results.setGridSize(QSize(self.THUMB + 30, self.THUMB + 70))
        self.results.setResizeMode(QListView.ResizeMode.Adjust)
        self.results.setMovement(QListView.Movement.Static)
        self.results.setWordWrap(True)
        self.results.setUniformItemSizes(True)
        self.results.itemDoubleClicked.connect(self._apply)
        self.results.currentItemChanged.connect(self._update_apply)

        self.status = QLabel()
        local = QPushButton("Local File…")
        local.clicked.connect(self._pick_file)
        self.apply_btn = QPushButton(f"Apply to {count} Track(s)")
        self.apply_btn.setDefault(True)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(local)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.apply_btn)
        bottom.addWidget(cancel)

        self.progress = ProgressStrip()
        self._loaded = 0

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.progress)
        layout.addWidget(self.results, 1)
        layout.addLayout(bottom)

        self._placeholder = QPixmap(self.THUMB, self.THUMB)
        self._placeholder.fill(QColor(128, 128, 128, 40))
        if album.strip():
            self.search()

    def search(self):
        album, artist = self.album.text().strip(), self.artist.text().strip()
        if not album:
            self.status.setText("Enter an album name to search.")
            return
        self.results.clear()
        self.status.clear()
        self._loaded = 0
        self.progress.start("Searching MusicBrainz")
        self.progress.set_detail(f"{album} — {artist}" if artist else album)

        def run(task):
            for event in covers.search_with_thumbnails(album, artist):
                task.progress.emit(event)

        self._task = Task(run)
        self._task.progress.connect(self._on_event)
        self._task.done.connect(self._on_done)
        self._task.failed.connect(self._on_failed)
        self._task.start()

    def _stale(self) -> bool:
        return self.sender() is not self._task

    def _on_event(self, event):
        if self._stale():
            return
        if event[0] == "releases":
            for rel in event[1]:
                item = QListWidgetItem(QIcon(self._placeholder), f"{rel['title']}\n{rel['artist']}\n{rel['details']}")
                item.setData(Qt.ItemDataRole.UserRole, rel["id"])
                item.setToolTip(f"{rel['title']}\n{rel['artist']}\n{rel['details']}\nMBID: {rel['id']}")
                self.results.addItem(item)
            if event[1]:
                self.progress.set_title("Loading covers")
                self.progress.set_detail(f"{len(event[1])} releases found")
                self.progress.set_progress(0, len(event[1]))
            else:
                self.status.setText("No releases found.")
        else:
            _, index, data = event
            self._loaded += 1
            self.progress.set_progress(self._loaded, self.results.count())
            item = self.results.item(index)
            pixmap = QPixmap()
            if data and pixmap.loadFromData(data):
                item.setIcon(QIcon(pixmap))
            else:
                item.setText(item.text() + "\n(no cover)")
                item.setFlags(Qt.ItemFlag.NoItemFlags)

    def _on_done(self, _):
        if self._stale():
            return
        self.progress.finish()
        total = self.results.count()
        with_art = sum(bool(self.results.item(i).flags() & Qt.ItemFlag.ItemIsEnabled) for i in range(total))
        if total:
            self.status.setText(f"{with_art} of {total} releases have cover art.")

    def _on_failed(self, message):
        if not self._stale():
            self.progress.finish()
            self.status.setText(f"Search failed: {message}")

    def _update_apply(self, item, _previous=None):
        self.apply_btn.setEnabled(bool(item and item.flags() & Qt.ItemFlag.ItemIsEnabled))

    def _apply(self, *_):
        item = self.results.currentItem()
        if item and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.source = ("mbid", item.data(Qt.ItemDataRole.UserRole))
            self.accept()

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose cover image", "", "Images (*.jpg *.jpeg *.png *.webp *.bmp)")
        if path:
            self.source = ("file", path)
            self.accept()


class MainWindow(QMainWindow):
    def __init__(self, folder: Path | None = None):
        super().__init__()
        self.setWindowTitle("expergo")
        self.resize(1300, 850)
        self.folder: Path | None = None
        self.worker: Worker | None = None

        self.model = TrackModel()
        self.model.dataChanged.connect(self._update_status)
        self.model.modelReset.connect(self._update_status)
        self.table = TrackTable(self.model)

        def action(text, slot, icon=None, shortcut=None, tip=None, checkable=False):
            act = QAction(text, self, checkable=checkable)
            if slot:
                act.triggered.connect(slot)
            if icon:
                act.setIcon(std_icon(*icon))
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            if tip:
                act.setToolTip(tip)
                act.setStatusTip(tip.replace("\n", " "))
            return act

        SP = QStyle.StandardPixmap
        # Library
        open_act = action("Open Folder…", self._pick_folder, ("document-open", SP.SP_DirOpenIcon), "Ctrl+O",
                          "Scan a folder recursively for FLAC files")
        rescan_act = action("Rescan", self.rescan, ("view-refresh", SP.SP_BrowserReload), "F5")
        save_act = action("Save", self.save_changes, ("document-save", SP.SP_DialogSaveButton), "Ctrl+S",
                          "Write edited tags / filenames to disk")
        add_act = action("Add Songs…", self.add_songs, ("list-add", SP.SP_FileDialogNewFolder), "Ctrl+N",
                         "Copy new FLACs into the library (optionally applying the Echo rules)")
        reorg_act = action("Reorganize…", self.reorganize, ("folder-new", SP.SP_FileDialogDetailedView),
                           "Ctrl+Shift+O", "Move every track in the library into Artist / Album folders (with preview)")
        # Processing
        rules_act = action("Apply Echo Rules", self.process, ("system-run", SP.SP_MediaPlay), "Ctrl+R",
                           "Rename, resample, fix blocksize, normalize, resize art, fetch LRC\n"
                           "(selected rows, or all visible rows if none selected)")
        lrc_act = action("Fetch Missing LRC", self.fetch_missing_lrc, ("emblem-downloads", SP.SP_ArrowDown), "Ctrl+L",
                         "Download LRC for rows with missing/empty lyrics\n"
                         "(selected rows, or all visible rows if none selected)")
        art_fetch_act = action("Fetch Missing Art", self.fetch_missing_art, ("folder-pictures", SP.SP_DirLinkIcon),
                               "Ctrl+Shift+E",
                               "Download the best-matching MusicBrainz cover for rows without album art,\n"
                               "once per album (selected rows, or all visible rows if none selected)")
        self.stop_action = action("Stop", lambda: self.worker and self.worker.stop(),
                                  ("process-stop", SP.SP_BrowserStop), tip="Stop after the files in progress")
        self.stop_action.setEnabled(False)
        # Album art (enabled only with a selection)
        self.art_edit_action = action("Edit Art…", self.edit_cover, ("image-x-generic", SP.SP_FileDialogContentsView),
                                      "Ctrl+E", "Pick album art from MusicBrainz / Cover Art Archive or a local file\n"
                                      "(applies to all selected rows)")
        self.art_remove_action = action("Remove", self.remove_cover, ("edit-delete", SP.SP_TrashIcon),
                                        tip="Remove embedded album art from the selected rows")
        self.art_actions = [self.art_edit_action, self.art_remove_action]
        # Options
        self.nolrc_action = action("Skip LRC Download", None, checkable=True,
                                   tip="Don't fetch lyrics when applying the Echo rules or importing")
        self.worker_group = QActionGroup(self)
        for n in (1, 2, 4, 8):
            act = action(f"{n} Worker{'s' if n > 1 else ''}", None, checkable=True)
            act.setData(n)
            act.setChecked(n == 1)
            self.worker_group.addAction(act)

        self.busy_actions = [open_act, rescan_act, save_act, add_act, reorg_act, rules_act, lrc_act, art_fetch_act]
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        file_menu.addActions([open_act, rescan_act, save_act])
        file_menu.addSeparator()
        file_menu.addActions([add_act, reorg_act])
        file_menu.addSeparator()
        file_menu.addAction(action("Quit", self.close, shortcut="Ctrl+Q"))
        tracks_menu = menubar.addMenu("&Tracks")
        tracks_menu.addActions([rules_act, lrc_act, art_fetch_act])
        tracks_menu.addSeparator()
        tracks_menu.addActions(self.art_actions)
        tracks_menu.addSeparator()
        tracks_menu.addAction(self.stop_action)
        options_menu = QMenu("&Options", self)
        options_menu.addAction(self.nolrc_action)
        options_menu.addSeparator()
        options_menu.addSection("Parallel Workers")
        options_menu.addActions(self.worker_group.actions())
        menubar.addMenu(options_menu)

        # Toolbar: the common actions, in three groups
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        tb.addActions([open_act, rescan_act, save_act])
        tb.addSeparator()
        tb.addActions([add_act, reorg_act])
        tb.addSeparator()
        tb.addAction(rules_act)
        rules_btn = tb.widgetForAction(rules_act)
        rules_btn.setMenu(options_menu)
        rules_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        tb.addActions([lrc_act, art_fetch_act])

        self.table.extra_actions = self.art_actions
        selection = self.table.selectionModel()
        selection.selectionChanged.connect(self._on_selection)
        selection.currentRowChanged.connect(self._on_selection)

        self.search = QLineEdit(placeholderText="Filter by any field…", clearButtonEnabled=True)
        self.search.textChanged.connect(lambda t: self.table.proxy.set_filter(text=t))
        self.missing_lrc = QCheckBox("Missing LRC")
        self.missing_lrc.toggled.connect(lambda c: self.table.proxy.set_filter(missing_lrc=c))
        self.missing_art = QCheckBox("Missing art")
        self.missing_art.toggled.connect(lambda c: self.table.proxy.set_filter(missing_art=c))
        filters = QHBoxLayout()
        filters.addWidget(self.search, 1)
        filters.addWidget(QLabel("Show only:"))
        filters.addWidget(self.missing_lrc)
        filters.addWidget(self.missing_art)

        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(10000)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(section_label("Log"))
        log_layout.addWidget(self.log_view)
        self.art_panel = ArtPanel(self.art_actions)

        bottom = QSplitter(Qt.Orientation.Horizontal)
        bottom.addWidget(log_box)
        bottom.addWidget(self.art_panel)
        bottom.setStretchFactor(0, 1)
        bottom.setSizes([1000, 280])

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(bottom)
        splitter.setSizes([550, 280])

        self.progress = ProgressStrip(self.stop_action)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(filters)
        layout.addWidget(self.progress)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self._on_selection()

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)

        missing = [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]
        if missing:
            self.log(f"WARNING: not found on PATH: {', '.join(missing)} · applying rules will fail.")

        self._update_status()
        if folder:
            self.load_folder(folder)

    def log(self, text: str):
        self.log_view.appendPlainText(text)

    def worker_count(self) -> int:
        return self.worker_group.checkedAction().data()

    def _on_selection(self, *_):
        tracks = self.table.selected_tracks()
        for act in self.art_actions:
            act.setEnabled(bool(tracks) and not self.worker)
        self.art_panel.show_track(self.table.current_track() if tracks else None, len(tracks))

    def _update_status(self, *_):
        dirty = sum(t.is_dirty() for t in self.model.tracks)
        missing = sum(t.lrc != "OK" for t in self.model.tracks)
        no_art = sum(not t.art for t in self.model.tracks)
        where = str(self.folder) if self.folder else "No folder open"
        self.status.setText(f"{where}  ·  {len(self.model.tracks)} tracks  ·  {missing} missing LRC  ·  "
                            f"{no_art} missing art  ·  {dirty} unsaved")
        self.setWindowTitle(("* " if dirty else "") + "expergo")

    def run_worker(self, title, items, fn, on_done=None, workers=None):
        self.worker = Worker(items, fn, workers or self.worker_count(), self)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self.progress.set_progress)
        self.worker.current.connect(lambda name: self.progress.set_detail(name))
        self.worker.finished.connect(lambda: self._on_worker_done(on_done))
        self._set_busy(True)
        self.progress.start(title, len(self.worker.items))
        self.worker.start()

    def _on_worker_done(self, on_done):
        worker, self.worker = self.worker, None
        self.progress.finish()
        self._set_busy(False)
        if on_done:
            on_done(worker)

    def _set_busy(self, busy: bool):
        for act in self.busy_actions:
            act.setEnabled(not busy)
        self.stop_action.setEnabled(busy)
        for act in self.art_actions:
            act.setEnabled(not busy and bool(self.table.selected_tracks()))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers if busy else EDIT_TRIGGERS)

    def _confirm_discard(self) -> bool:
        if not any(t.is_dirty() for t in self.model.tracks):
            return True
        answer = QMessageBox.question(self, "Unsaved changes", "Discard unsaved tag edits?")
        return answer == QMessageBox.StandardButton.Yes

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Open music folder", str(self.folder or ""))
        if folder:
            self.load_folder(Path(folder))

    def load_folder(self, folder: Path):
        if not self._confirm_discard():
            return
        self.folder = Path(folder).resolve()
        files = find_flacs([self.folder])
        self.log(f"Scanning {self.folder} ({len(files)} FLAC files)…")

        def done(worker):
            self.model.set_tracks(sorted((t for _, t in worker.results), key=lambda t: str(t.path).lower()))
            self.table.fit_columns()

        self.run_worker("Scanning library", files, Track.load, done, workers=4)

    def rescan(self):
        if self.folder:
            self.load_folder(self.folder)

    def save_changes(self, tracks=None) -> bool:
        ok = True
        for track in [t for t in (tracks or self.model.tracks) if t.is_dirty()]:
            try:
                save_track(track)
                self.log(f"Saved {track.name}")
            except Exception as e:
                ok = False
                self.log(f"ERROR saving {track.name}: {e}")
        self.model.refresh()
        return ok

    def process(self):
        tracks = self.table.target_tracks()
        if not tracks or not tools_ok(self):
            return
        answer = QMessageBox.question(
            self,
            "Apply Echo rules",
            f"Apply Echo formatting rules to {len(tracks)} file(s)?\n\n"
            "Files are renamed, re-encoded, loudness-normalized and have their album art resized IN PLACE.\n"
            "Unsaved edits on these rows are saved first.",
        )
        if answer != QMessageBox.StandardButton.Yes or not self.save_changes(tracks):
            return
        core._claimed_names.clear()
        nolrc = self.nolrc_action.isChecked()
        self.run_worker("Applying Echo rules", [t.path for t in tracks], lambda p: core.process_file(p, nolrc),
                        lambda _: self.rescan())

    def fetch_missing_lrc(self):
        tracks = [t for t in self.table.target_tracks() if t.lrc != "OK"]
        if not tracks:
            self.log("No missing LRC files in the current selection.")
            return

        def done(_):
            for t in tracks:
                t.refresh_lrc()
            self.model.refresh()
            self.table.proxy.invalidate()

        self.log(f"Fetching LRC for {len(tracks)} track(s)…")
        self.run_worker("Fetching lyrics", tracks, fetch_lrc, done)

    def _refresh_art(self, tracks):
        for t in tracks:
            t.refresh_art()
        self.model.refresh()
        self.table.proxy.invalidate()
        self._on_selection()

    def fetch_missing_art(self):
        groups: dict[tuple[str, str], list[Track]] = {}
        skipped = 0
        for t in self.table.target_tracks():
            if t.art:
                continue
            if not t.album_key[1]:
                skipped += 1
                continue
            groups.setdefault(t.album_key, []).append(t)
        if skipped:
            self.log(f"Skipped {skipped} track(s) without an Album tag (use Edit Art… for those)")
        if not groups:
            self.log("No tracks with missing album art in the current selection.")
            return
        items = [
            SimpleNamespace(key=key, tracks=tracks, name=f"{key[1]} — {key[0]}" if key[0] else key[1])
            for key, tracks in groups.items()
        ]
        tracks = [t for group in items for t in group.tracks]
        self.log(f"Fetching album art for {len(items)} album(s) ({len(tracks)} tracks)…")
        self.run_worker("Fetching album art", items, auto_cover, lambda _: self._refresh_art(tracks))

    def edit_cover(self):
        tracks = self.table.selected_tracks()
        if not tracks:
            return
        first = self.table.current_track() or tracks[0]
        dialog = CoverDialog(first.tags["ALBUM"], first.tags["ALBUMARTIST"] or first.tags["ARTIST"], len(tracks), self)
        if dialog.exec() and dialog.source:
            source = dialog.source
            self.log(f"Applying album art ({source[0]}: {source[1]}) to {len(tracks)} track(s)…")
            self.run_worker("Applying album art", [source], lambda s: apply_cover(s, tracks),
                            lambda _: self._refresh_art(tracks), workers=1)
            origin = "Cover Art Archive" if source[0] == "mbid" else Path(source[1]).name
            self.progress.set_detail(f"Downloading from {origin} and embedding into {len(tracks)} track(s)")
        dialog.deleteLater()

    def remove_cover(self):
        tracks = self.table.selected_tracks()
        if not tracks:
            return
        answer = QMessageBox.question(self, "Remove album art", f"Remove embedded album art from {len(tracks)} track(s)?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        def remove(track):
            covers.write_cover(track.path, None)
            return f"Removed album art from {track.name}"

        self.run_worker("Removing album art", tracks, remove, lambda _: self._refresh_art(tracks))

    def reorganize(self):
        if not self.folder or not self.model.tracks:
            self.log("Open a library folder first.")
            return
        if any(t.is_dirty() for t in self.model.tracks):
            answer = QMessageBox.question(
                self, "Reorganize", "Folders are based on the tags saved on disk. Save your unsaved edits first?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Ignore | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Save and not self.save_changes():
                return
        dialog = ReorganizeDialog(self.model.tracks, self.folder, self)
        accepted = dialog.exec()
        moves, cleanup = dialog.moves, dialog.cleanup.isChecked()
        dialog.deleteLater()
        if not accepted or not moves:
            return

        library_root = self.folder

        def done(worker):
            moved = len(worker.results)
            msg = f"Moved {moved} of {len(moves)} file(s) into Artist / Album folders"
            if cleanup and library_root:
                msg += f", removed {remove_empty_dirs(library_root)} empty folder(s)"
            self.log(msg)
            self.rescan()

        self.log(f"Reorganizing {len(moves)} file(s)…")
        self.run_worker("Reorganizing folders", moves, move_track, done, workers=1)

    def add_songs(self):
        dialog = ImportDialog(self.folder, self.worker_count(), self.nolrc_action.isChecked(), self)
        dialog.imported.connect(self.rescan)
        dialog.exec()
        dialog.deleteLater()

    def closeEvent(self, event):
        if self.worker:
            QMessageBox.information(self, "Busy", "A task is still running. Stop it first.")
            event.ignore()
        elif self._confirm_discard():
            event.accept()
        else:
            event.ignore()


def main():
    app = QApplication(sys.argv)
    window = MainWindow(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
