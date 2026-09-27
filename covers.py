import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

import requests
from mutagen.flac import FLAC, Picture
from PIL import Image

HEADERS = {"User-Agent": "SnowskyEchoFlacTool/0.1 ( https://github.com/pinapelz/fiio-snowsky-echo-flac-tool )"}
MB_SEARCH_URL = "https://musicbrainz.org/ws/2/release/"
CAA_URL = "https://coverartarchive.org/release/{mbid}/front{size}"
COVER_SIZE = 500

# MusicBrainz allows ~1 request/second per client
_mb_lock = threading.Lock()
_mb_last = 0.0


def _quote(text: str) -> str:
    return '"' + text.replace("\\", " ").replace('"', " ").strip() + '"'


def search_releases(album: str, artist: str = "", limit: int = 25) -> list[dict]:
    global _mb_last
    query = f"release:{_quote(album)}"
    if artist.strip():
        query += f" AND artist:{_quote(artist)}"
    with _mb_lock:
        time.sleep(max(0.0, _mb_last + 1.1 - time.monotonic()))
        try:
            resp = requests.get(
                MB_SEARCH_URL,
                params={"query": query, "fmt": "json", "limit": limit},
                headers=HEADERS,
                timeout=15,
            )
        finally:
            _mb_last = time.monotonic()
    resp.raise_for_status()

    releases = []
    for rel in resp.json().get("releases", []):
        credit = "".join(c.get("name", "") + c.get("joinphrase", "") for c in rel.get("artist-credit", []))
        formats = ", ".join(sorted({m["format"] for m in rel.get("media", []) if m.get("format")}))
        details = " · ".join(x for x in (rel.get("date", "")[:4], rel.get("country", ""), formats) if x)
        releases.append({"id": rel["id"], "title": rel.get("title", ""), "artist": credit, "details": details})
    return releases


def fetch_cover(mbid: str, size: int | None = 250) -> bytes | None:
    url = CAA_URL.format(mbid=mbid, size=f"-{size}" if size else "")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def search_with_thumbnails(album: str, artist: str):
    releases = search_releases(album, artist)
    yield "releases", releases

    def thumb(mbid):
        try:
            return fetch_cover(mbid, 250)
        except requests.RequestException:
            return None

    executor = ThreadPoolExecutor(max_workers=6)
    try:
        futures = {executor.submit(thumb, rel["id"]): i for i, rel in enumerate(releases)}
        for future in as_completed(futures):
            yield "thumb", futures[future], future.result()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def find_best_cover(album: str, artist: str, tries: int = 5) -> tuple[bytes, dict] | None:
    """Front cover of the best-ranked matching release that has one, as (image bytes, release)."""
    for rel in search_releases(album, artist, limit=tries):
        data = fetch_cover(rel["id"], 1200) or fetch_cover(rel["id"], None)
        if data:
            return data, rel
    return None


def load_source(source: tuple[str, str]) -> bytes:
    kind, value = source
    if kind == "file":
        return Path(value).read_bytes()
    for size in (1200, 500, None):
        data = fetch_cover(value, size)
        if data:
            return data
    raise LookupError(f"No cover art found for release {value}")


def prepare_cover(data: bytes, size: int = COVER_SIZE) -> bytes:
    with Image.open(BytesIO(data)) as img:
        resized = img.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
    out = BytesIO()
    resized.save(out, format="JPEG", quality=92)
    return out.getvalue()


def read_cover(path: Path) -> Picture | None:
    pictures = FLAC(str(path)).pictures
    return next((p for p in pictures if p.type == 3), pictures[0] if pictures else None)


def write_cover(path: Path, jpeg: bytes | None) -> None:
    audio = FLAC(str(path))
    audio.clear_pictures()
    if jpeg:
        with Image.open(BytesIO(jpeg)) as img:
            width, height = img.size
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.width, pic.height, pic.depth = width, height, 24
        pic.data = jpeg
        audio.add_picture(pic)
    audio.save()
