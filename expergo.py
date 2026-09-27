import argparse
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import covers
import syncedlyrics
from mutagen.flac import FLAC
from tqdm import tqdm

_lyrics_semaphore = threading.Semaphore(3)

_claimed_names: set[Path] = set()
_claimed_names_lock = threading.Lock()


def iter_files(base: Path) -> Iterator[Path]:
    for p in base.rglob("*"):
        if p.is_file():
            yield p.resolve()


def find_flacs(base_dir: Path) -> list[Path]:
    return sorted(p for p in iter_files(base_dir) if p.suffix.lower() == ".flac" and not p.name.endswith(".tmp.flac"))


def sanitize_filename(name: str) -> str:
    illegal = r'\\/:*?"<>|'
    return "".join(c for c in name if c not in illegal).strip()


def rename_file(filepath: Path, new_name: str) -> Path:
    target = filepath.with_name(new_name)
    filepath.rename(target)
    lrc, new_lrc = filepath.with_suffix(".lrc"), target.with_suffix(".lrc")
    if lrc.exists() and not new_lrc.exists():
        lrc.rename(new_lrc)
    return target


def get_tags(path: Path) -> dict[str, str]:
    audio = FLAC(str(path))
    return {
        "title": audio.get("TITLE", [""])[0],
        "artist": audio.get("ARTIST", [""])[0],
        "album": audio.get("ALBUM", [""])[0],
        "albumartist": audio.get("ALBUMARTIST", [""])[0],
    }


def has_cover(path: Path) -> bool:
    try:
        return bool(FLAC(str(path)).pictures)
    except Exception:
        return False


def download_lrc(lrc_path: Path, title: str, artist: str) -> bool:
    with _lyrics_semaphore:
        lrc = syncedlyrics.search(f"{title} {artist}", providers=["Lrclib", "Megalobiz", "NetEase"])
        time.sleep(0.3)

    with open(lrc_path, "w", encoding="utf-8") as f:
        f.write(lrc if lrc else "")
    return bool(lrc)


def get_audio_issues(path: Path) -> dict:
    audio = FLAC(str(path))
    info = audio.info
    sample_rate = getattr(info, "sample_rate", 0)
    bits_per_sample = getattr(info, "bits_per_sample", 24)
    max_blocksize = getattr(info, "max_blocksize", 4096)

    return {
        "needs_sample_rate_fix": sample_rate > 192000,
        "needs_bitdepth_fix": bits_per_sample > 24,
        "sample_rate": sample_rate,
        "bits_per_sample": bits_per_sample,
        "max_blocksize": max_blocksize,
    }


def fix_with_ffmpeg(path: Path, fix_sample_rate: bool, fix_bitdepth: bool) -> Path:
    args = [
        "ffmpeg", "-y",
        "-i", str(path),
        "-map", "0:a",
        "-map", "0:v?",
        "-c:v", "copy",
        "-acodec", "flac",
    ]
    if fix_bitdepth:
        args += ["-sample_fmt", "s24"]
    if fix_sample_rate:
        args += ["-ar", "192000"]

    temp_path = path.with_suffix(".tmp.flac")
    args.append(str(temp_path))
    subprocess.run(args, capture_output=True, text=True, check=True)
    path.unlink()
    temp_path.rename(path)
    return path


def fix_blocksize(path: Path, blocksize: int = 4096) -> Path:
    temp_path = path.with_suffix(".tmp.flac")
    command = ["flac", "--force", f"--blocksize={blocksize}", str(path), "-o", str(temp_path)]
    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
        path.unlink()
        temp_path.rename(path)
    except subprocess.CalledProcessError as e:
        temp_path.unlink(missing_ok=True)
        print(f"  Error fixing blocksize for {path.name}:")
        print(e.stderr)
    return path


def resize_album_art(path: Path) -> None:
    audio = FLAC(str(path))
    if not audio.pictures:
        return
    from io import BytesIO
    from PIL import Image

    for pic in audio.pictures:
        with Image.open(BytesIO(pic.data)) as img:
            resized = img.resize((500, 500), Image.Resampling.LANCZOS)
            out = BytesIO()
            fmt = img.format if img.format else "PNG"
            resized.save(out, format=fmt)
            pic.data = out.getvalue()
    audio.save()


def normalize_loudness(path: Path, target_lufs: float = -14.0, target_tp: float = -1.0) -> Path:
    temp_path = path.with_suffix(".tmp.flac")
    subprocess.run(
        [
            "ffmpeg-normalize", str(path),
            "-o", str(temp_path),
            "-c:a", "flac",
            "-t", str(target_lufs),
            "--true-peak", str(target_tp),
            "-f",
            "-e", "-map 0:v? -c:v copy",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    path.unlink()
    temp_path.rename(path)
    return path


def process_file(fp: Path, nolrc: bool) -> str:
    lines = []
    log = lines.append

    log(f"\nProcessing: {fp.name}")
    tags = get_tags(fp)
    title, artist, album = tags["title"], tags["artist"], tags["album"]

    if not title:
        log("  Warning: TITLE tag is empty, using filename as title")
        title = fp.stem
        artist = "UNKNOWN ARTIST"

    new_stem = sanitize_filename(f"{title} - {artist}")
    with _claimed_names_lock:
        target = fp.with_name(new_stem + ".flac")
        conflict = (target.resolve() in _claimed_names) or (
            target.exists() and target.resolve() != fp.resolve()
        )
        if conflict:
            if album:
                log("  Conflict detected, adding album name as differentiator")
                new_stem = sanitize_filename(f"{title} - {artist} ({album})")
            else:
                log("  Warning: filename conflict but no album tag, keeping original name")
                new_stem = fp.stem
        new_file_name = new_stem + ".flac"
        _claimed_names.add(fp.with_name(new_file_name).resolve())

    if new_file_name != fp.name:
        fp = rename_file(fp, new_file_name)

    issues = get_audio_issues(fp)
    log(f"  Stats: {issues['sample_rate']}Hz, {issues['bits_per_sample']}-bit, blocksize={issues['max_blocksize']}")

    if issues["needs_sample_rate_fix"] or issues["needs_bitdepth_fix"]:
        reasons = []
        if issues["needs_sample_rate_fix"]:
            reasons.append(f"sample rate {issues['sample_rate']}Hz -> 192000Hz")
        if issues["needs_bitdepth_fix"]:
            reasons.append(f"bit depth {issues['bits_per_sample']}-bit -> 24-bit")
        log(f"  Fixing via ffmpeg: {', '.join(reasons)}")
        fp = fix_with_ffmpeg(fp, issues["needs_sample_rate_fix"], issues["needs_bitdepth_fix"])

    log("  Normalizing loudness to -14 LUFS")
    fp = normalize_loudness(fp)

    post_blocksize = getattr(FLAC(str(fp)).info, "max_blocksize", 4096)
    if post_blocksize > 4096:
        log("  Fixing blocksize -> 4096 via flac CLI")
        fp = fix_blocksize(fp)

    log("  Resizing album art to 500x500")
    resize_album_art(fp)

    if nolrc:
        return "\n".join(lines)

    if not title or not artist:
        log(f"  Skipping LRC for {fp.name} (missing title or artist tag)")
        return "\n".join(lines)

    lrc_path = fp.with_suffix(".lrc")
    if lrc_path.exists():
        log(f"  Skipping LRC for {fp.name} (already exists)")
        return "\n".join(lines)

    log(f"  Fetching LRC for: {title} - {artist}")
    download_lrc(lrc_path, title, artist)
    return "\n".join(lines)


def auto_lrc_file(fp: Path, force: bool = False) -> str:
    tags = get_tags(fp)
    title, artist = tags["title"].strip(), tags["artist"].strip()
    if not title or not artist:
        return f"Skip {fp.name}: missing TITLE or ARTIST"

    lrc_path = fp.with_suffix(".lrc")
    if lrc_path.exists() and not force:
        return f"Skip {fp.name}: LRC already exists"

    found = download_lrc(lrc_path, title, artist)
    return f"{'Fetched' if found else 'No'} LRC: {title} - {artist}"


@dataclass
class AlbumGroup:
    key: tuple[str, str]
    tracks: list[Path]

    @property
    def name(self) -> str:
        artist, album = self.key
        return f"{album} - {artist}" if artist else album


def auto_cover_group(group: AlbumGroup, tries: int = 5) -> str:
    artist, album = group.key
    found = covers.find_best_cover(album, artist, tries=tries)
    if not found:
        return f"No cover found: {group.name}"

    data, rel = found
    jpeg = covers.prepare_cover(data)
    for fp in group.tracks:
        covers.write_cover(fp, jpeg)

    info = " · ".join(x for x in (rel["title"], rel["artist"], rel["details"]) if x)
    return f"Cover applied to {len(group.tracks)} track(s) of {group.name} (from {info})"


@dataclass
class MoveItem:
    src: Path
    dst: Path
    conflict: bool

    @property
    def name(self) -> str:
        return self.src.name


def safe_dirname(name: str, fallback: str) -> str:
    return sanitize_filename(name).rstrip(". ") or fallback


class FolderLayout:
    def __init__(self, root: Path, prefer_album_artist: bool = True):
        self.root = root
        self.prefer_album_artist = prefer_album_artist
        self._names: dict[Path, dict[str, str]] = {}

    def _canonical(self, parent: Path, name: str) -> str:
        if parent not in self._names:
            existing = {}
            if parent.is_dir():
                existing = {c.name.lower(): c.name for c in parent.iterdir() if c.is_dir()}
            self._names[parent] = existing
        return self._names[parent].setdefault(name.lower(), name)

    def folder_for(self, tags: dict[str, str]) -> Path:
        artist = tags["albumartist"] if self.prefer_album_artist else ""
        artist = (artist or tags["artist"] or tags["albumartist"]).strip()
        artist_dir = self._canonical(self.root, safe_dirname(artist, "Unknown Artist"))
        album_dir = self._canonical(self.root / artist_dir, safe_dirname(tags["album"].strip(), "Unknown Album"))
        return self.root / artist_dir / album_dir


def plan_reorg(files: list[Path], root: Path, prefer_album_artist: bool) -> list[MoveItem]:
    layout = FolderLayout(root, prefer_album_artist)
    claimed: set[str] = set()
    plan = []
    for fp in files:
        dst = layout.folder_for(get_tags(fp)) / fp.name
        if dst == fp:
            continue
        key = str(dst).lower()
        conflict = key in claimed or dst.exists()
        claimed.add(key)
        plan.append(MoveItem(src=fp, dst=dst, conflict=conflict))
    return plan


def move_item(item: MoveItem) -> str:
    if item.dst.exists():
        raise FileExistsError(f"{item.dst} already exists")
    item.dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(item.src, item.dst)
    lrc, new_lrc = item.src.with_suffix(".lrc"), item.dst.with_suffix(".lrc")
    if lrc.exists() and not new_lrc.exists():
        shutil.move(lrc, new_lrc)
    return ""


def remove_empty_dirs(root: Path) -> int:
    removed = 0
    for dirpath, _, _ in os.walk(root, topdown=False):
        path = Path(dirpath)
        try:
            if path != root and not any(path.iterdir()):
                path.rmdir()
                removed += 1
        except OSError:
            pass
    return removed


def run_jobs(items: list, worker: Callable, workers: int, desc: str) -> tuple[int, int]:
    ok = errors = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(worker, item): item for item in items}
        with tqdm(total=len(items), desc=desc, unit="item") as pbar:
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                    if isinstance(result, str) and result.strip():
                        tqdm.write(result)
                    ok += 1
                except Exception as e:
                    name = getattr(item, "name", Path(item).name if isinstance(item, Path) else str(item))
                    tqdm.write(f"ERROR {name}: {e}")
                    errors += 1
                finally:
                    pbar.update(1)
    return ok, errors


def command_process(base_dir: Path, workers: int, nolrc: bool) -> int:
    files = find_flacs(base_dir)
    _claimed_names.clear()
    ok, errors = run_jobs(files, lambda fp: process_file(fp, nolrc), workers, "Applying Echo rules")
    tqdm.write(f"Done: {ok} processed, {errors} errors")
    return 0 if errors == 0 else 1


def command_auto_lrc(base_dir: Path, workers: int, force: bool) -> int:
    files = find_flacs(base_dir)
    ok, errors = run_jobs(files, lambda fp: auto_lrc_file(fp, force=force), workers, "Fetching LRC")
    tqdm.write(f"Done: {ok} checked, {errors} errors")
    return 0 if errors == 0 else 1


def command_auto_cover(base_dir: Path, workers: int, force: bool, tries: int) -> int:
    files = find_flacs(base_dir)
    groups: dict[tuple[str, str], list[Path]] = {}
    skipped = 0

    for fp in files:
        if not force and has_cover(fp):
            continue
        tags = get_tags(fp)
        album = tags["album"].strip()
        artist = (tags["albumartist"] or tags["artist"]).strip()
        if not album:
            skipped += 1
            continue
        groups.setdefault((artist, album), []).append(fp)

    items = [AlbumGroup(key=key, tracks=tracks) for key, tracks in groups.items()]
    if skipped:
        tqdm.write(f"Skipped {skipped} track(s) without an ALBUM tag")
    if not items:
        tqdm.write("Nothing to do (no albums matched)")
        return 0

    ok, errors = run_jobs(items, lambda g: auto_cover_group(g, tries=tries), workers, "Fetching album art")
    tqdm.write(f"Done: {ok} albums checked, {errors} errors")
    return 0 if errors == 0 else 1


def command_reorg(base_dir: Path, workers: int, prefer_album_artist: bool, cleanup: bool) -> int:
    files = find_flacs(base_dir)
    plan = plan_reorg(files, base_dir, prefer_album_artist)
    moves = [m for m in plan if not m.conflict]
    conflicts = len(plan) - len(moves)

    tqdm.write(f"Plan: {len(moves)} move(s), {len(files) - len(plan)} already in place, {conflicts} conflict(s)")
    if not moves:
        return 0

    ok, errors = run_jobs(moves, move_item, workers, "Reorganizing")
    if cleanup:
        removed = remove_empty_dirs(base_dir)
        tqdm.write(f"Removed {removed} empty folder(s)")
    tqdm.write(f"Done: {ok} moved, {errors} errors")
    return 0 if errors == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="expergo CLI")
    sub = parser.add_subparsers(dest="command")

    p_process = sub.add_parser("process", help="Apply full Echo rules")
    p_process.add_argument("base_dir", type=Path)
    p_process.add_argument("--nolrc", action="store_true", dest="nolrc")
    p_process.add_argument("-n", "--workers", type=int, default=1)

    p_lrc = sub.add_parser("auto-lrc", help="Fetch missing LRC files")
    p_lrc.add_argument("base_dir", type=Path)
    p_lrc.add_argument("-n", "--workers", type=int, default=1)
    p_lrc.add_argument("--force", action="store_true", help="Overwrite existing .lrc files")

    p_cover = sub.add_parser("auto-cover", help="Fetch album art from MusicBrainz/Cover Art Archive")
    p_cover.add_argument("base_dir", type=Path)
    p_cover.add_argument("-n", "--workers", type=int, default=1)
    p_cover.add_argument("--force", action="store_true", help="Also replace existing embedded cover art")
    p_cover.add_argument("--tries", type=int, default=5, help="Release search candidates per album (default: 5)")

    p_reorg = sub.add_parser("reorg", help="Move tracks into Artist/Album folders")
    p_reorg.add_argument("base_dir", type=Path)
    p_reorg.add_argument("-n", "--workers", type=int, default=1)
    p_reorg.add_argument("--artist-only", action="store_true", help="Use ARTIST only (ignore ALBUMARTIST)")
    p_reorg.add_argument("--no-cleanup", action="store_true", help="Do not remove empty folders after moving")

    return parser


def normalize_legacy_argv(argv: list[str]) -> list[str]:
    commands = {"process", "auto-lrc", "auto-cover", "reorg", "-h", "--help"}
    if len(argv) > 1 and argv[1] not in commands and not argv[1].startswith("-"):
        return [argv[0], "process", *argv[1:]]
    return argv


def main() -> int:
    import sys

    argv = normalize_legacy_argv(list(sys.argv))
    if "gui" in argv:
        import gui
        gui.main()
    args = build_parser().parse_args(argv[1:])

    base_dir = getattr(args, "base_dir", None)
    if base_dir and not base_dir.is_dir():
        raise SystemExit(f"Not a directory: {base_dir}")

    match args.command:
        case "process":
            return command_process(args.base_dir, args.workers, args.nolrc)
        case "auto-lrc":
            return command_auto_lrc(args.base_dir, args.workers, args.force)
        case "auto-cover":
            return command_auto_cover(args.base_dir, args.workers, args.force, max(1, args.tries))
        case "reorg":
            return command_reorg(args.base_dir, args.workers, not args.artist_only, not args.no_cleanup)
        case _:
            build_parser().print_help()
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
