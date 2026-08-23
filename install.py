#!/usr/bin/env python3
"""Install googletasks2zim into Zim's plugin folder.

Installs the pip dependencies, then copies the plugin files into Zim's
plugins directory, flattening `_lib/zim_page_content.py` next to
`googletasks.py` as required by Zim's plugin loader (see CLAUDE.md).

Usage: python3 install.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

FILES_TO_COPY = [
    REPO_ROOT / "googletasks.py",
    REPO_ROOT / "_lib" / "zim_page_content.py",
    REPO_ROOT / "googletasks_client_id.json",
]


def zim_plugins_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            sys.exit("APPDATA environment variable not set; cannot locate Zim's plugin folder.")
        return Path(appdata) / "zim" / "data" / "zim" / "plugins"
    xdg_data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg_data_home) / "zim" / "plugins"


def main():
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REPO_ROOT / "requirements.txt"), "--break-system-packages"],
        check=True,
    )

    dest = zim_plugins_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for src in FILES_TO_COPY:
        shutil.copy2(src, dest / src.name)
        print(f"Copied {src.name} -> {dest / src.name}")

    print(
        "\nDone. Now in Zim: enable 'Google tasks' in Edit / Preferences / Plugins, "
        "then configure it in File / Properties / Google Tasks."
    )


if __name__ == "__main__":
    main()
