from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from models import Track, plan_reorganize
from ui_common import MISSING_BRUSH


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
