import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path


CUETOOLS_VERSION = "2.2.6"
CUETOOLS_DOWNLOAD_URL = (
    "https://github.com/gchudov/cuetools.net/releases/download/"
    "v2.2.6/CUETools_2.2.6.zip"
)


def _get_powershell_executable() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def _get_windows_tools_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "fiio-snowsky-echo-flac-tool" / "tools"
    return Path.home() / ".fiio-snowsky-echo-flac-tool" / "tools"


def _get_windows_cuetools_dir() -> Path:
    return _get_windows_tools_root() / f"cuetools-{CUETOOLS_VERSION}"


def _find_cueripper_in_dir(base_dir: Path) -> str | None:
    if not base_dir.exists():
        return None

    for name in ("CUERipper.Console.exe", "CUERipper.exe"):
        matches = sorted(base_dir.rglob(name))
        if matches:
            return str(matches[0])

    return None


def _get_cueripper_executable() -> str | None:
    explicit = os.environ.get("CUERIPPER_EXE")
    if explicit:
        return explicit

    for name in ["CUERipper.Console.exe", "CUERipper.exe"]:
        found = shutil.which(name)
        if found:
            return found

    return _find_cueripper_in_dir(_get_windows_cuetools_dir())


def needs_cuetools_install() -> bool:
    return platform.system() == "Windows" and _get_cueripper_executable() is None


def ensure_windows_cuetools(
    on_output: Callable[[str], None] | None = None,
) -> str:
    if platform.system() != "Windows":
        raise RuntimeError("CUETools bootstrap is only supported on Windows")

    existing = _get_cueripper_executable()
    if existing:
        return existing

    install_dir = _get_windows_cuetools_dir()
    install_dir.mkdir(parents=True, exist_ok=True)

    zip_path = install_dir / "CUETools_2.2.6.zip"

    if on_output:
        on_output(f"Downloading CUETools from {CUETOOLS_DOWNLOAD_URL}")

    with urllib.request.urlopen(CUETOOLS_DOWNLOAD_URL) as resp, zip_path.open("wb") as dst:
        shutil.copyfileobj(resp, dst)

    if on_output:
        on_output(f"Extracting {zip_path} to {install_dir}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(install_dir)

    cueripper = _find_cueripper_in_dir(install_dir)
    if not cueripper:
        raise RuntimeError(
            "Downloaded CUETools archive but could not find CUERipper executable"
        )

    if on_output:
        on_output(f"CUERipper installed: {cueripper}")

    return cueripper


def _iter_existing_dirs(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists() and p.is_dir():
            out.append(p)
    return out


def _find_recent_flacs(search_roots: list[Path], since_epoch: float) -> list[str]:
    found: list[str] = []
    for root in _iter_existing_dirs(search_roots):
        for path in root.rglob("*.flac"):
            try:
                if path.stat().st_mtime >= since_epoch:
                    found.append(str(path.resolve()))
            except OSError:
                continue
    return sorted(set(found))


def _run_streaming_command(
    cmd: list[str],
    cwd: Path,
    on_output: Callable[[str], None] | None = None,
) -> list[str]:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )

    lines: list[str] = []
    if proc.stdout:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if on_output and line:
                on_output(line)

    rc = proc.wait()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd, output="\n".join(lines))

    return lines


def check_dependencies() -> tuple[bool, str]:
    if platform.system() == "Windows":
        missing: list[str] = []
        if _get_powershell_executable() is None:
            missing.append("powershell")
        if _get_cueripper_executable() is None:
            missing.append("CUERipper.Console.exe")

        if missing:
            return (
                False,
                "Ripping CDs on Windows requires: "
                + ", ".join(missing)
                + ". CUERipper can be auto-downloaded.",
            )
        return True, "OK"

    if platform.system() == "Linux":
        try:
            subprocess.run(["abcde", "-h"], check=True, capture_output=True)
            return True, "OK"
        except (subprocess.CalledProcessError, FileNotFoundError):
            return (
                False,
                "Ripping CDs requires the 'abcde' package to be installed, please refer to the README for more information",
            )

    return True, "You are on an unsupported platform, CD features may not be fully functional"


def get_optical_drives() -> list[tuple[str, str]]:
    import re

    if platform.system() == "Windows":
        powershell = _get_powershell_executable()
        if not powershell:
            return []

        try:
            res = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_CDROMDrive | "
                    "Select-Object Drive, Caption, Name, MediaLoaded | "
                    "ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            payload = res.stdout.strip()
            if not payload:
                return []

            parsed = json.loads(payload)
            entries = parsed if isinstance(parsed, list) else [parsed]

            drives: list[tuple[str, str]] = []
            for entry in entries:
                drive = entry.get("Drive")
                if not drive:
                    continue
                caption = entry.get("Caption") or entry.get("Name") or drive
                media_loaded = entry.get("MediaLoaded")
                if media_loaded is False:
                    caption = f"{caption} (no media)"
                drives.append((drive, caption))

            return drives
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return []

    if platform.system() == "Linux":
        unix_path_regex = re.compile(r"(/dev/[^\s]+)")
        try:
            res = subprocess.run(
                ["lsblk", "-d", "-o", "PATH,TYPE,MODEL"],
                capture_output=True,
                text=True,
                check=True,
            )
            drives = [
                (m.group(1), line)
                for line in res.stdout.splitlines()
                if "rom" in line and (m := unix_path_regex.search(line))
            ]
            return drives
        except subprocess.CalledProcessError:
            return []

    return []


def run_abcde_rip(
    drive: str,
    on_output: Callable[[str], None] | None = None,
) -> list[str]:
    workspace = Path(tempfile.gettempdir()) / "expergo-rip-workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Windows":
        cueripper = _get_cueripper_executable()
        if not cueripper:
            raise RuntimeError(
                "CUERipper executable not found. Install CUETools and ensure "
                "CUERipper.Console.exe is in PATH, set CUERIPPER_EXE, "
                "or use auto-download from the Rip CD prompt."
            )

        is_console = Path(cueripper).name.lower() == "cueripper.console.exe"
        rip_start = time.time()

        if is_console:
            cmd = [
                cueripper,
                "--drive",
                drive,
                "--output",
                str(workspace),
                "--format",
                "flac",
            ]
        else:
            if on_output:
                on_output("CUERipper GUI opened. Start rip there and close it when done.")
            cmd = [cueripper]

        _run_streaming_command(cmd, workspace, on_output)

        flacs_in_workspace = sorted(str(p.resolve()) for p in workspace.rglob("*.flac"))
        if flacs_in_workspace:
            return flacs_in_workspace

        configured_output = os.environ.get("CUERIPPER_OUTPUT_DIR")
        search_roots = [
            workspace,
            Path(cueripper).parent,
            Path.home() / "Music",
            Path.home() / "Documents",
            Path.home() / "Desktop",
            Path.home() / "Downloads",
            Path(tempfile.gettempdir()),
        ]
        if configured_output:
            search_roots.insert(0, Path(configured_output))

        recent_flacs = _find_recent_flacs(search_roots, rip_start - 2)
        if recent_flacs:
            return recent_flacs

        searched = "\n".join(f"- {p}" for p in _iter_existing_dirs(search_roots))
        raise RuntimeError(
            "CUERipper completed but no FLAC files were detected.\n\n"
            "Searched these locations for newly created .flac files:\n"
            f"{searched}\n\n"
            "If CUERipper saves elsewhere, set CUERIPPER_OUTPUT_DIR to that folder."
        )

    cmd = ["abcde", "-d", drive, "-L", "-n", "-N", "-o", "flac"]
    _run_streaming_command(cmd, workspace, on_output)

    return sorted(str(p.resolve()) for p in workspace.rglob("*.flac"))
