from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import covers
from ui_common import ProgressStrip, Task, section_label


class AlbumInfoDialog(QDialog):
    def __init__(self, album: str, artist: str, count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Album Info · {count} track(s)")
        self.resize(900, 640)
        self.result_info: dict | None = None
        self._task: Task | None = None
        self._releases: list[dict] = []

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
        self.results.setAlternatingRowColors(True)
        self.results.itemDoubleClicked.connect(self._apply)
        self.results.currentItemChanged.connect(self._on_release_changed)

        self.tracks = QTableWidget(0, 3)
        self.tracks.setHorizontalHeaderLabels(["#", "Title", "Artist"])
        self.tracks.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tracks.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tracks.verticalHeader().setVisible(False)
        self.tracks.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tracks.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        lists = QSplitter(Qt.Orientation.Horizontal)
        releases_box = QWidget()
        releases_layout = QVBoxLayout(releases_box)
        releases_layout.setContentsMargins(0, 0, 0, 0)
        releases_layout.addWidget(section_label("Releases"))
        releases_layout.addWidget(self.results)
        tracks_box = QWidget()
        tracks_layout = QVBoxLayout(tracks_box)
        tracks_layout.setContentsMargins(0, 0, 0, 0)
        tracks_layout.addWidget(section_label("Tracklist"))
        tracks_layout.addWidget(self.tracks)
        lists.addWidget(releases_box)
        lists.addWidget(tracks_box)
        lists.setSizes([380, 520])

        self.tag_titles = QCheckBox("Tag titles / artists by track number")
        self.tag_titles.setChecked(True)
        self.tag_titles.setToolTip("Match each row's track number against the MusicBrainz tracklist\n"
                                   "and fill in TITLE and ARTIST")
        self.status = QLabel()
        self.apply_btn = QPushButton(f"Apply to {count} Track(s)")
        self.apply_btn.setDefault(True)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.tag_titles)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.apply_btn)
        bottom.addWidget(cancel)

        self.progress = ProgressStrip()

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.progress)
        layout.addWidget(lists, 1)
        layout.addLayout(bottom)

        if album.strip():
            self.search()

    def search(self):
        album, artist = self.album.text().strip(), self.artist.text().strip()
        if not album:
            self.status.setText("Enter an album name to search.")
            return
        self.results.clear()
        self.tracks.setRowCount(0)
        self._releases = []
        self.status.clear()
        self.apply_btn.setEnabled(False)
        self.progress.start("Searching MusicBrainz")
        self.progress.set_detail(f"{album} - {artist}" if artist else album)

        def run(_task):
            return covers.search_releases(album, artist)

        self._task = Task(run)
        self._task.done.connect(self._on_results)
        self._task.failed.connect(self._on_failed)
        self._task.start()

    def _stale(self) -> bool:
        return self.sender() is not self._task

    def _on_results(self, releases):
        if self._stale():
            return
        self.progress.finish()
        self._releases = list(releases)
        for rel in self._releases:
            label = f"{rel['title']} — {rel['artist']}"
            item = QListWidgetItem(f"{label}\n{rel['details']}" if rel["details"] else label)
            item.setToolTip(f"{label}\n{rel['details']}\nMBID: {rel['id']}")
            self.results.addItem(item)
        if self._releases:
            self.status.setText(f"{len(self._releases)} release(s) found.")
            self.results.setCurrentRow(0)
        else:
            self.status.setText("No releases found.")

    def _on_failed(self, message):
        if not self._stale():
            self.progress.finish()
            self.status.setText(f"Search failed: {message}")

    def _current_release(self) -> dict | None:
        row = self.results.currentRow()
        return self._releases[row] if 0 <= row < len(self._releases) else None

    def _on_release_changed(self, *_):
        release = self._current_release()
        self.tracks.setRowCount(0)
        self.apply_btn.setEnabled(bool(release))
        if not release:
            return
        self.progress.start("Loading tracklist")
        self.progress.set_detail(release["title"])
        mbid = release["id"]

        def run(_task):
            return mbid, covers.fetch_release_tracks(mbid)

        self._task = Task(run)
        self._task.done.connect(self._on_tracks)
        self._task.failed.connect(self._on_failed)
        self._task.start()

    def _on_tracks(self, payload):
        if self._stale():
            return
        self.progress.finish()
        mbid, tracks = payload
        release = self._current_release()
        if not release or release["id"] != mbid:
            return
        self.tracks.setRowCount(len(tracks))
        for row, track in enumerate(tracks):
            number = f"{track['disc']}-{track['number']}" if track["disc"] > 1 else str(track["number"])
            for col, text in enumerate((number, track["title"], track["artist"])):
                self.tracks.setItem(row, col, QTableWidgetItem(text))
        self.status.setText(f"{len(tracks)} track(s) in {release['title']}.")

    def _apply(self, *_):
        release = self._current_release()
        if not release:
            return
        by_number: dict[int, tuple[str, str]] = {}
        for row in range(self.tracks.rowCount()):
            number_item = self.tracks.item(row, 0)
            title_item = self.tracks.item(row, 1)
            artist_item = self.tracks.item(row, 2)
            if not number_item:
                continue
            number = number_item.text().split("-")[-1]
            if number.isdigit():
                by_number[int(number)] = (title_item.text() if title_item else "",
                                          artist_item.text() if artist_item else "")
        self.result_info = {
            "album": release["title"],
            "album_artist": release["artist"],
            "mbid": release["id"],
            "tag_tracks": self.tag_titles.isChecked(),
            "by_number": by_number,
        }
        self.accept()
