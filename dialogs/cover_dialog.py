from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

import covers
from ui_common import ProgressStrip, Task


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
        self.progress.set_detail(f"{album} - {artist}" if artist else album)

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
