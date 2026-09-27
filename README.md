# expergo
An opinionated music library tool to help you manage your FLACs, compatible with the Snowsky Echo/Mini.

> [!IMPORTANT]
> expergo works entirely in the directory you've pointed it to. It can work off of removable storage such as the Snowsky Echo Mini, but be aware that large jobs can take some time (limited by read-write speeds).

# Echo Rules
These are the rules that the CLI applies (can be OPTIONALLY applied from the GUI):
- Recursively searches through the provided directory
- Re-samples audio higher than 192Khz 24bit via ffmpeg down to 192Khz
- Re-encodes files with block size higher than 4096 via `flac` CLI
- Rename FLAC file to `TRACK_NAME - ARTIST.flac`
- Resize embedded album art to 500x500px (good enough for small screen players, without taking up much space)
- Normalize all audio to -14 LUFS
- Download LRC files (places them in same folder with same name, according to Snowsky Echo rules)
  - A delay of 0.3 sec is hardcoded to avoid rate limit, this is a hard limit across all threads (if running multithreaded)

# External Dependencies
You need the FLAC command line tool to be accessible globally, meaning it must be able to run anywhere on your machine. Using the official tool was the most consistent way of fixing the block-size issue cross-platform.

https://xiph.org/flac/download.html

- Windows: `winget install -e --id Xiph.FLAC`
- Linux: `sudo pacman -S flac` (follow your package manager)
- macOS: `brew install flac` (idk tho i don't own a mac)

All other dependencies can be installed via `uv`
```bash
uv sync
```

# CLI

**Apply Echo Rules:**
```bash
uv run expergo.py <base_dir> [--nolrc] [-n workers]
```

**Subcommands:**
```bash
uv run expergo.py process <base_dir> [--nolrc] [-n workers]
uv run expergo.py auto-lrc <base_dir> [--force] [-n workers]
uv run expergo.py auto-cover <base_dir> [--force] [--tries N] [-n workers]
uv run expergo.py reorg <base_dir> [--artist-only] [--no-cleanup] [-n workers]
```

What each command does:
- `process`: full Echo rules pipeline (rename, resample/re-encode, normalize, resize art, optional LRC)
- `auto-lrc`: only fetch LRC files for FLACs (skips existing by default, `--force` overwrites)
- `auto-cover`: only fetch album art using MusicBrainz + Cover Art Archive, grouped per album (uses album artist when present)
- `reorg`: reorganize library into `Artist/Album/` folders, moving matching `.lrc` sidecars too
- 
# GUI
```bash
uv run gui.py [base_dir]
```

- **Open Folder** scans recursively and lists every FLAC: filename, title, artist, album, album artist, genre and LRC status
- Double-click a cell to edit it. Edited cells are highlighted until you **Save Changes** (Ctrl+S). To edit many rows at once, right-click and choose "Set … for N rows"
- **Apply Echo Rules** runs the full script pipeline above on the selected rows, or on all visible rows if nothing is selected
- The LRC column shows `Missing` (no `.lrc` with the same name) or `Empty` (a lookup found nothing). Tick **Only missing LRC** to filter to those rows, then use **Fetch Missing LRC** to fill them in
- The **Album Art** panel beside the log shows the embedded cover of the highlighted row. **Edit Art** (Ctrl+E) searches MusicBrainz and shows covers from the Cover Art Archive to choose from. You can also pick a local image. The chosen cover is resized to 500x500 and embedded in every selected row, so select a whole album at once
- The **Art** column flags tracks without embedded album art. Tick **Missing art** to filter to them. **Fetch Missing Art** (Ctrl+Shift+E) looks up each album once on MusicBrainz and embeds the best-ranked cover it finds in every track of that album. Tracks without an Album tag are skipped, so use **Edit Art** for those
- The toolbar holds the common actions. The **File / Tracks / Options** menus contain everything, including the "Skip LRC" option and the parallel worker count, which are also in the arrow menu next to **Apply Echo Rules**
- **Add Songs** opens a window where you drop in new FLACs, fix their tags, then copy them into the library (optionally into `Artist/Album/` folders). The rules are applied to the copies unless you untick **Apply Echo rules**, in which case the files are copied as-is with only your tag edits (and LRC unless skipped). The original files are left alone
- **Reorganize** (Ctrl+Shift+O) moves every FLAC in the open folder (with its `.lrc`) into `Artist/Album/` folders. It uses Album Artist when set, so compilations stay together. It shows a preview of every move first. Filenames are kept, clashing files are skipped rather than overwritten, and empty folders can be removed afterwards

# Other Tools
`apply_album_art.sh` - Recursively traverse all folders, automatically apply album art and resize to all FLACs that have a JPG/PNG in the same directory
