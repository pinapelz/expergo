import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from PyQt6.QtCore import (
    QAbstractTableModel,
    QElapsedTimer,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QBrush, QColor, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QStyle,
    QTableView,
    QToolButton,
)

from models import ART_COL, FIELDS, HEADERS, LRC_COL, Track

DIRTY_BRUSH = QBrush(QColor(255, 190, 0, 70))
MISSING_BRUSH = QBrush(QColor(220, 60, 60))
EDIT_TRIGGERS = (
    QAbstractItemView.EditTrigger.DoubleClicked
    | QAbstractItemView.EditTrigger.EditKeyPressed
    | QAbstractItemView.EditTrigger.AnyKeyPressed
)
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
        self.detail.setStyleSheet("color: palette(placeholder-text);")
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


def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setStyleSheet("font-weight: bold; font-size: 11px; color: palette(placeholder-text); padding: 2px 0;")
    return label
