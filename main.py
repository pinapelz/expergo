import argparse
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator
from tqdm import tqdm
import syncedlyrics
from mutagen.flac import FLAC

_lyrics_semaphore = threading.Semaphore(3)

_claimed_names: set[Path] = set()
_claimed_names_lock = threading.Lock()


def iter_files(base: Path) -> Iterator[Path]:
    iterator = base.rglob("*")
    for p in iterator:
        if p.is_file():
            yield p.resolve()


def rename_file(filepath: Path, new_name: str) -> Path:
    target = filepath.with_name(new_name)
    filepath.rename(target)
    lrc, new_lrc = filepath.with_suffix(".lrc"), target.with_suffix(".lrc")
    if lrc.exists() and not new_lrc.exists():
        lrc.rename(new_lrc)
    return target


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
        "needs_blocksize_fix": max_blocksize > 4096,
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


def get_track_info(path: Path) -> tuple:
    audio = FLAC(str(path))
    title = audio.get("TITLE", [""])
    artist = audio.get("ARTIST", [""])
    album = audio.get("ALBUM", [""])
    return (title[0], artist[0], album[0])


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


def sanitize_filename(name: str) -> str:
    illegal = r'\/:*?"<>|'
    return "".join(c for c in name if c not in illegal).strip()


def process_file(fp: Path, nolrc: bool) -> str:
    lines = []
    log = lines.append

    log(f"\nProcessing: {fp.name}")
    title, artist, album = get_track_info(fp)

    if not title:
        log(f"  Warning: TITLE tag is empty, using filename as title")
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
                log(f"  Conflict detected, adding album name as differentiator")
                new_stem = sanitize_filename(f"{title} - {artist} ({album})")
            else:
                log(f"  Warning: filename conflict but no album tag, keeping original name")
                new_stem = fp.stem
        new_file_name = new_stem + ".flac"
        _claimed_names.add(fp.with_name(new_file_name).resolve())
    if new_file_name != fp.name:
        rename_file(fp, new_file_name)
        fp = fp.with_name(new_file_name)

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
        log(f"  Fixing blocksize -> 4096 via flac CLI")
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_dir", type=Path)
    parser.add_argument("--nolrc", action="store_true", dest="nolrc")
    parser.add_argument("-n", "--workers", type=int, default=1, dest="workers",
                        help="Number of parallel workers (default: 1)")
    args = parser.parse_args()

    files = [p for p in iter_files(args.base_dir) if p.suffix == ".flac"]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_file, fp, args.nolrc): fp for fp in files}
        with tqdm(total=len(files), desc="Processing FLAC files", unit="file") as pbar:
            for future in as_completed(futures):
                try:
                    result = future.result()
                    tqdm.write(result)
                except Exception as e:
                    fp = futures[future]
                    tqdm.write(f"\n  ERROR processing {fp.name}: {e}")
                finally:
                    pbar.update(1)


if __name__ == "__main__":
    main()
