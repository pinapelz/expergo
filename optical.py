import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path


def check_dependencies() -> tuple[bool, str]:
    if platform.system() == "Windows":
        return False, "Windows is currently not supported"  # stub for now
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
        return []  # stub for now
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
    workspace = Path("/tmp/expergo-rip-workspace")
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    cmd = ["abcde", "-d", drive, "-L", "-n", "-N", "-o", "flac"]
    proc = subprocess.Popen(
        cmd,
        cwd=workspace,
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

    return sorted(str(p.resolve()) for p in workspace.rglob("*.flac"))
