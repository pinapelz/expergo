import shutil
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyle,
    QVBoxLayout,
)

import expergo as core
from models import (
    FolderLayout,
    Track,
    find_flacs,
    flac_name,
    track_number,
    write_tags,
)
from ui_common import ProgressStrip, TrackModel, TrackTable, Worker, std_icon, tools_ok

from .album_info_dialog import AlbumInfoDialog


class ImportDialog(QDialog):
    imported = pyqtSignal()

    def __init__(self, dest: Path | None, workers: int, nolrc: bool, replaygain: bool = False, parent=None):
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
        mb_info = QPushButton("MusicBrainz Album Info…")
        mb_info.clicked.connect(self._musicbrainz_album_info)
        top = QHBoxLayout()
        for w in (add_files, add_folder, remove, mb_info):
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
        self.replaygain = QCheckBox("Use ReplayGain")
        self.replaygain.setChecked(replaygain)
        self.replaygain.setToolTip("Write ReplayGain tags instead of destructively normalizing the audio.\n"
                                   "Only effective if your player supports ReplayGain.")
        self.replaygain.setEnabled(self.apply_rules.isChecked())
        self.apply_rules.toggled.connect(self.replaygain.setEnabled)
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.setValue(workers)
        opts = QHBoxLayout()
        opts.addWidget(self.apply_rules)
        opts.addWidget(self.organize)
        opts.addWidget(self.nolrc)
        opts.addWidget(self.replaygain)
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

    def _musicbrainz_album_info(self):
        tracks = self.table.selected_tracks() or self.model.tracks
        if not tracks:
            QMessageBox.information(self, "Add Songs", "Add at least one track first.")
            return

        first = self.table.current_track() or tracks[0]
        dialog = AlbumInfoDialog(first.tags["ALBUM"], (first.tags["ALBUMARTIST"] or first.tags["ARTIST"]),
                                 len(tracks), self)
        accepted = dialog.exec()
        info = dialog.result_info
        dialog.deleteLater()
        if not accepted or not info:
            return

        album, album_artist = info["album"].strip(), info["album_artist"].strip()
        by_number, tagged, unmatched = info["by_number"], 0, []
        for track in tracks:
            if album:
                track.tags["ALBUM"] = album
            if album_artist:
                track.tags["ALBUMARTIST"] = album_artist
            if not info["tag_tracks"]:
                continue
            number = track_number(track.path)
            match = by_number.get(number) if number else None
            if match:
                title, artist = match
                if title:
                    track.tags["TITLE"] = title
                if artist:
                    track.tags["ARTIST"] = artist
                tagged += 1
            else:
                unmatched.append(track.name)

        self.model.refresh()
        self.log.appendPlainText(f"Applied album info from MusicBrainz ({info['mbid']}) to {len(tracks)} track(s): "
                                 f"ALBUM='{album}', ALBUMARTIST='{album_artist}'")
        if info["tag_tracks"]:
            self.log.appendPlainText(f"  Tagged {tagged} title(s)/artist(s) by track number")
            for name in unmatched:
                self.log.appendPlainText(f"  No tracklist match for {name} (missing/unknown track number)")

    def _import_one(self, track: Track, dest_root: Path, layout: FolderLayout | None,
                    apply_rules: bool, nolrc: bool, replaygain: bool) -> str:
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
            return f"Copied to {dest_dir}" + core.process_file(target.resolve(), nolrc, replaygain)

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
        apply_rules, nolrc, replaygain = (self.apply_rules.isChecked(), self.nolrc.isChecked(),
                                          self.replaygain.isChecked())
        if apply_rules and not tools_ok(self):
            return
        core._claimed_names.clear()
        self.import_btn.setEnabled(False)
        dest_root = Path(self.dest.text().strip()).expanduser().resolve()
        layout = FolderLayout(dest_root) if self.organize.isChecked() else None
        fn = lambda t: self._import_one(t, dest_root, layout, apply_rules, nolrc, replaygain)
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
