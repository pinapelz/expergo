from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from mutagen.flac import FLAC

import covers
import expergo as core

FIELDS = [
    ("FILE", "Filename"),
    ("TITLE", "Title"),
    ("ARTIST", "Artist"),
    ("ALBUM", "Album"),
    ("ALBUMARTIST", "Album Artist"),
    ("GENRE", "Genre"),
]
TAG_KEYS = [key for key, _ in FIELDS[1:]]
LRC_COL = len(FIELDS)
ART_COL = LRC_COL + 1
HEADERS = [label for _, label in FIELDS] + ["LRC", "Art"]


def lrc_status(path: Path) -> str:
    lrc = path.with_suffix(".lrc")
    if not lrc.exists():
        return "Missing"
    return "OK" if lrc.stat().st_size > 0 else "Empty"


def find_flacs(paths) -> list[Path]:
    found = []
    for p in map(Path, paths):
        if p.is_dir():
            found += core.find_flacs(p)
        else:
            r = p.resolve()
            if r.is_file() and r.suffix.lower() == ".flac" and not r.name.endswith(".tmp.flac"):
                found.append(r)
    return sorted(set(found))


def flac_name(name: str) -> str:
    name = core.sanitize_filename(name)
    return name if name.lower().endswith(".flac") else name + ".flac"


@dataclass
class Track:
    path: Path
    orig: dict
    tags: dict
    lrc: str
    art: bool

    @classmethod
    def load(cls, path: Path) -> Track:
        audio = FLAC(str(path))
        orig = {"FILE": path.name, **{k: audio.get(k, [""])[0] for k in TAG_KEYS}}
        return cls(path, orig, dict(orig), lrc_status(path), bool(audio.pictures))

    @property
    def name(self) -> str:
        return self.path.name

    def is_dirty(self, key: str | None = None) -> bool:
        return self.tags[key] != self.orig[key] if key else self.tags != self.orig

    def refresh_lrc(self) -> None:
        self.lrc = lrc_status(self.path)

    def refresh_art(self) -> None:
        try:
            self.art = bool(FLAC(str(self.path)).pictures)
        except Exception:
            self.art = False

    @property
    def album_key(self) -> tuple[str, str]:
        return (self.tags["ALBUMARTIST"] or self.tags["ARTIST"]).strip(), self.tags["ALBUM"].strip()


def write_tags(path: Path, track: Track) -> None:
    """Write only edited tags so untouched multi-value tags are preserved."""
    changed = [k for k in TAG_KEYS if track.is_dirty(k)]
    if not changed:
        return
    audio = FLAC(str(path))
    for key in changed:
        value = track.tags[key].strip()
        if value:
            audio[key] = value
        elif key in audio:
            del audio[key]
    audio.save()


def save_track(track: Track) -> None:
    write_tags(track.path, track)
    if track.is_dirty("FILE"):
        name = flac_name(track.tags["FILE"])
        if name == ".flac":
            raise ValueError("filename cannot be empty")
        target = track.path.with_name(name)
        if target.exists() and not target.samefile(track.path):
            raise FileExistsError(f"{name} already exists")
        track.path = core.rename_file(track.path, name)
    track.tags["FILE"] = track.path.name
    track.orig = dict(track.tags)
    track.refresh_lrc()


class FolderLayout:
    """GUI adapter over main.FolderLayout (GUI tags use uppercase keys)."""

    def __init__(self, root: Path, prefer_album_artist: bool = True):
        self._core = core.FolderLayout(root, prefer_album_artist)

    def folder_for(self, tags: dict) -> Path:
        return self._core.folder_for({
            "artist": tags.get("ARTIST", ""),
            "albumartist": tags.get("ALBUMARTIST", ""),
            "album": tags.get("ALBUM", ""),
        })


def plan_reorganize(tracks: list[Track], root: Path, prefer_album_artist: bool) -> list[SimpleNamespace]:
    plan = core.plan_reorg([t.path for t in tracks], root, prefer_album_artist)
    names = {t.path: t.name for t in tracks}
    return [SimpleNamespace(name=names.get(m.src, m.src.name), src=m.src, dst=m.dst, conflict=m.conflict) for m in plan]


def move_track(item: SimpleNamespace) -> str:
    return core.move_item(core.MoveItem(src=item.src, dst=item.dst, conflict=False))


def remove_empty_dirs(root: Path) -> int:
    return core.remove_empty_dirs(root)


def fetch_lrc(track: Track) -> str:
    return core.auto_lrc_file(track.path, force=False)


def apply_cover(source: tuple[str, str], tracks: list[Track]) -> str:
    jpeg = covers.prepare_cover(covers.load_source(source))
    for track in tracks:
        covers.write_cover(track.path, jpeg)
    return f"Applied album art to {len(tracks)} track(s)"


def auto_cover(group) -> str:
    return core.auto_cover_group(core.AlbumGroup(key=group.key, tracks=[t.path for t in group.tracks]))
