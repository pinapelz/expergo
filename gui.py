import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStyle,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

import covers
import expergo as core
from dialogs import CoverDialog, ImportDialog, ReorganizeDialog, RipCdDialog
from models import (
    Track,
    apply_cover,
    auto_cover,
    fetch_lrc,
    find_flacs,
    move_track,
    remove_empty_dirs,
    save_track,
)
from ui_common import (
    EDIT_TRIGGERS,
    REQUIRED_TOOLS,
    ProgressStrip,
    Task,
    TrackModel,
    TrackTable,
    Worker,
    format_duration,
    section_label,
    std_icon,
    tools_ok,
)

os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")


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
        self.info.setStyleSheet("color: palette(placeholder-text);")

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


class AudioPlayerPanel(QFrame):
    def __init__(self, logger=None, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self._log = logger or (lambda _msg: None)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(False)
        self.audio.setVolume(1.0)
        self.player.setAudioOutput(self.audio)

        self._duration = 0
        self._current_path: Path | None = None

        self.title = QLabel("No track loaded")
        self.title.setStyleSheet("font-weight: bold;")
        self.time = QLabel("0:00 / 0:00")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)

        self.play_btn = QToolButton()
        self.play_btn.setIcon(std_icon("media-playback-start", QStyle.StandardPixmap.SP_MediaPlay))
        self.play_btn.setToolTip("Play / Pause")
        self.stop_btn = QToolButton()
        self.stop_btn.setIcon(std_icon("media-playback-stop", QStyle.StandardPixmap.SP_MediaStop))
        self.stop_btn.setToolTip("Stop")

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(section_label("Preview"))
        row.addWidget(self.title, 1)
        row.addWidget(self.play_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.time)
        row.addWidget(self.slider, 2)

        self.play_btn.clicked.connect(self.toggle_play_pause)
        self.stop_btn.clicked.connect(self.stop)
        self.slider.sliderMoved.connect(self.player.setPosition)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_media_status)

    def current_path(self) -> Path | None:
        return self._current_path

    def load(self, path: Path, autoplay: bool = True):
        self._current_path = path
        self.title.setText(path.name)
        self.audio.setMuted(False)
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.setPosition(0)
        if autoplay:
            self.player.play()

    def toggle_play_pause(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self):
        self.player.stop()

    def clear_if_matches(self, path: Path):
        if self._current_path and self._current_path == path:
            self.stop()
            self._current_path = None
            self.title.setText("No track loaded")
            self._duration = 0
            self.slider.setRange(0, 0)
            self.time.setText("0:00 / 0:00")

    def _on_position(self, pos: int):
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self.time.setText(f"{format_duration(pos / 1000)} / {format_duration(self._duration / 1000)}")

    def _on_duration(self, ms: int):
        self._duration = max(0, ms)
        self.slider.setRange(0, self._duration)
        self.time.setText(f"{format_duration(self.player.position() / 1000)} / {format_duration(self._duration / 1000)}")

    def _on_state(self, state):
        icon = ("media-playback-pause", QStyle.StandardPixmap.SP_MediaPause) if state == QMediaPlayer.PlaybackState.PlayingState else ("media-playback-start", QStyle.StandardPixmap.SP_MediaPlay)
        self.play_btn.setIcon(std_icon(*icon))

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.InvalidMedia and self._current_path:
            self._log(f"Preview failed: unsupported/unreadable media: {self._current_path}")

    def _on_error(self, _error, error_text: str):
        if error_text:
            self.title.setText(f"Preview error: {error_text}")
            self._log(f"Preview error: {error_text}")


class MainWindow(QMainWindow):
    def __init__(self, folder: Path | None = None):
        super().__init__()
        self.setWindowTitle("expergo")
        self.resize(1300, 850)
        self.folder: Path | None = None
        self.worker: Worker | None = None
        self.rip_task: Task | None = None
        self.rip_drive: str | None = None

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
        rip_cd_act = action("Rip CD", self.rip_cd, ("media-optical", SP.SP_DirOpenIcon),
                            tip="Rip tracks from an audio CD")
        self.preview_action = action("Preview", self.preview_track, ("media-playback-start", SP.SP_MediaPlay),
                                     tip="Play/pause the selected track in the embedded player")
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
        self.delete_action = action("Delete File(s)…", self.delete_selected_tracks, ("edit-delete", SP.SP_TrashIcon), "Delete",
                                    "Delete selected audio files from disk and remove them from the table")
        self.art_actions = [self.art_edit_action, self.art_remove_action]
        # Options
        self.nolrc_action = action("Skip LRC Download", None, checkable=True,
                                   tip="Don't fetch lyrics when applying the Echo rules or importing")
        self.replaygain_action = action("Use ReplayGain Instead of Normalizing", None, checkable=True,
                                        tip="Write ReplayGain tags instead of destructively normalizing the audio\n"
                                            "(only effective if your player supports ReplayGain)")
        self.worker_group = QActionGroup(self)
        for n in (1, 2, 4, 8):
            act = action(f"{n} Worker{'s' if n > 1 else ''}", None, checkable=True)
            act.setData(n)
            act.setChecked(n == 1)
            self.worker_group.addAction(act)

        self.busy_actions = [open_act, rescan_act, save_act, add_act, reorg_act, rip_cd_act, rules_act, lrc_act,
                             art_fetch_act, self.preview_action, self.delete_action]
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        file_menu.addActions([open_act, rescan_act, save_act])
        file_menu.addSeparator()
        file_menu.addActions([add_act, reorg_act, rip_cd_act])
        file_menu.addSeparator()
        file_menu.addAction(action("Quit", self.close, shortcut="Ctrl+Q"))
        tracks_menu = menubar.addMenu("&Tracks")
        tracks_menu.addAction(self.preview_action)
        tracks_menu.addAction(self.delete_action)
        tracks_menu.addSeparator()
        tracks_menu.addActions([rules_act, lrc_act, art_fetch_act])
        tracks_menu.addSeparator()
        tracks_menu.addActions(self.art_actions)
        tracks_menu.addSeparator()
        tracks_menu.addAction(self.stop_action)
        options_menu = QMenu("&Options", self)
        options_menu.addAction(self.nolrc_action)
        options_menu.addAction(self.replaygain_action)
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
        tb.addActions([add_act, reorg_act, rip_cd_act])
        tb.addSeparator()
        tb.addAction(self.preview_action)
        tb.addAction(self.delete_action)
        tb.addAction(rules_act)
        rules_btn = tb.widgetForAction(rules_act)
        rules_btn.setMenu(options_menu)
        rules_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        tb.addActions([lrc_act, art_fetch_act])

        self.table.extra_actions = [self.preview_action, self.delete_action, self.art_edit_action]
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
        self.player_panel = AudioPlayerPanel(self.log, self)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(filters)
        layout.addWidget(self.progress)
        layout.addWidget(self.player_panel)
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
        enabled = bool(tracks) and not self.worker
        self.preview_action.setEnabled(enabled)
        self.delete_action.setEnabled(enabled)
        for act in self.art_actions:
            act.setEnabled(enabled)
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
        selected = bool(self.table.selected_tracks())
        self.preview_action.setEnabled(not busy and selected)
        self.delete_action.setEnabled(not busy and selected)
        for act in self.art_actions:
            act.setEnabled(not busy and selected)
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

    def _selected_source_rows(self) -> list[int]:
        rows = self.table.selected_rows()
        if not rows and self.table.currentIndex().isValid():
            rows = [self.table.proxy.mapToSource(self.table.currentIndex()).row()]
        return rows

    def delete_selected_tracks(self):
        rows = self._selected_source_rows()
        if not rows:
            self.log("Select at least one track to delete.")
            return
        tracks = [self.model.tracks[r] for r in rows]
        answer = QMessageBox.warning(
            self,
            "Delete file(s)",
            f"Delete {len(tracks)} file(s) from disk?\n\n"
            "This permanently deletes the audio files and any sidecar .lrc files.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        removed_rows: list[int] = []
        errors: list[str] = []
        removed_files = removed_lrc = 0
        for row, track in zip(rows, tracks):
            path = track.path
            try:
                if path.exists():
                    if not path.is_file():
                        raise RuntimeError("path is not a file")
                    path.unlink()
                    removed_files += 1
                lrc_path = path.with_suffix(".lrc")
                if lrc_path.exists() and lrc_path.is_file():
                    lrc_path.unlink()
                    removed_lrc += 1
                removed_rows.append(row)
                self.player_panel.clear_if_matches(path)
            except Exception as e:
                errors.append(f"{track.name}: {e}")

        if removed_rows:
            self.model.remove_rows(removed_rows)
            cleaned = remove_empty_dirs(self.folder) if self.folder else 0
            self.log(f"Deleted {len(removed_rows)} track(s) from table ({removed_files} audio, {removed_lrc} LRC), removed {cleaned} empty folder(s).")
            self._update_status()
            self._on_selection()
        if errors:
            for err in errors:
                self.log(f"ERROR deleting {err}")
            QMessageBox.warning(self, "Delete finished with errors", "\n".join(errors[:8]))

    def preview_track(self):
        track = self.table.current_track() or (self.table.selected_tracks()[0] if self.table.selected_tracks() else None)
        if not track:
            self.log("Select a track to preview.")
            return
        if not track.path.exists():
            self.log(f"Preview failed: file does not exist: {track.path}")
            return
        if self.player_panel.current_path() == track.path:
            self.player_panel.toggle_play_pause()
        else:
            self.player_panel.load(track.path, autoplay=True)

    def process(self):
        tracks = self.table.target_tracks()
        if not tracks or not tools_ok(self):
            return
        replaygain = self.replaygain_action.isChecked()
        loudness = "tagged with ReplayGain" if replaygain else "loudness-normalized"
        answer = QMessageBox.question(
            self,
            "Apply Echo rules",
            f"Apply Echo formatting rules to {len(tracks)} file(s)?\n\n"
            f"Files are renamed, re-encoded, {loudness} and have their album art resized IN PLACE.\n"
            "Unsaved edits on these rows are saved first.",
        )
        if answer != QMessageBox.StandardButton.Yes or not self.save_changes(tracks):
            return
        core._claimed_names.clear()
        nolrc = self.nolrc_action.isChecked()
        self.run_worker("Applying Echo rules", [t.path for t in tracks],
                        lambda p: core.process_file(p, nolrc, replaygain),
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
            SimpleNamespace(key=key, tracks=tracks, name=f"{key[1]} - {key[0]}" if key[0] else key[1])
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
        dialog = ImportDialog(self.folder, self.worker_count(), self.nolrc_action.isChecked(),
                              self.replaygain_action.isChecked(), self)
        dialog.imported.connect(self.rescan)
        dialog.exec()
        dialog.deleteLater()

    def rip_cd(self):
        import optical

        if self.worker or self.rip_task:
            QMessageBox.information(self, "Busy", "A task is still running. Stop it first.")
            return

        ok, msg = optical.check_dependencies()
        if not ok:
            if optical.needs_cuetools_install():
                answer = QMessageBox.question(
                    self,
                    "Rip CD",
                    (
                        "CUERipper was not found.\n\n"
                        "Download and install CUETools now?\n\n"
                        f"Source: {optical.CUETOOLS_DOWNLOAD_URL}"
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    try:
                        optical.ensure_windows_cuetools(on_output=self.log)
                        ok, msg = optical.check_dependencies()
                    except Exception as err:
                        QMessageBox.critical(self, "Rip CD", f"Failed to install CUETools.\n\n{err}")
                        return

            if not ok:
                QMessageBox.critical(self, "Rip CD", msg)
                return

        drives = optical.get_optical_drives()
        if not drives:
            QMessageBox.information(self, "Rip CD", "No optical drives found.")
            self.log("Rip CD: no optical drives found.")
            return

        dialog = RipCdDialog(drives, self)
        accepted = dialog.exec()
        selected_drive = dialog.selected_drive
        dialog.deleteLater()
        if not accepted or not selected_drive:
            return

        self.rip_drive = selected_drive
        self.log(f"Rip CD selected drive: {self.rip_drive}")
        self.log("Starting CD ripping process…")

        self._set_busy(True)
        self.stop_action.setEnabled(False)
        self.progress.start("Ripping CD", 1)
        self.progress.set_detail(f"Ripping from {selected_drive}, Don't worry if the metadata is off you'll have a chance to fix it")

        def run(task):
            return optical.run_abcde_rip(
                selected_drive,
                on_output=lambda line: task.progress.emit(line),
            )

        self.rip_task = Task(run)
        self.rip_task.progress.connect(lambda line: self.log(str(line)))

        def done(flac_files):
            self.rip_task = None
            self.progress.finish()
            self._set_busy(False)
            if flac_files:
                self.log(f"Rip complete: {len(flac_files)} FLAC file(s).")
                for path in flac_files:
                    self.log(path)

                dialog = ImportDialog(self.folder, self.worker_count(), self.nolrc_action.isChecked(),
                                      self.replaygain_action.isChecked(), self)
                dialog.imported.connect(self.rescan)
                dialog.add_paths(flac_files)
                dialog.exec()
                dialog.deleteLater()
            else:
                self.log("Rip complete: no FLAC files were found in output.")

        def failed(err):
            self.rip_task = None
            self.progress.finish()
            self._set_busy(False)
            self.log(f"ERROR ripping CD: {err}")
            QMessageBox.critical(self, "Rip CD", f"Ripping failed.\n\n{err}")

        self.rip_task.done.connect(done)
        self.rip_task.failed.connect(failed)
        self.rip_task.start()

    def closeEvent(self, event):
        if self.worker or self.rip_task:
            QMessageBox.information(self, "Busy", "A task is still running. Stop it first.")
            event.ignore()
        elif self._confirm_discard():
            self.player_panel.stop()
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
