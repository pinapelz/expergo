# expergo
#### An opinionated music library tool to help you manage your FLACs, compatible with the Snowsky Echo/Mini.

<img width="1920" alt="image" src="https://github.com/user-attachments/assets/6e09e90c-1406-43ef-a1d8-fa99e1c73588" />

---

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
uv run expergo.py gui
```

> [!TIP]
> If you're using a Snowsky device, you can "emulate" playlist functionality by grouping songs together via the "Genre" tag

# Other Tools
`apply_album_art.sh` - Recursively traverse all folders, automatically apply album art and resize to all FLACs that have a JPG/PNG in the same directory
