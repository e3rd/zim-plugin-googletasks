#!/usr/bin/env python3
"""Install googletasks2zim into Zim's plugin folder.

Installs the pip dependencies, then copies the plugin files into Zim's
plugins directory, flattening `_lib/zim_page_content.py` next to
`googletasks.py` as required by Zim's plugin loader (see CLAUDE.md).

Usage:
    python3 install.py              install deps and copy the files
    python3 install.py --link       symlink the files instead of copying (edits in the repo apply
                                    after restarting Zim, handy for development)
    python3 install.py --uninstall  remove the installed plugin files (pip dependencies, OAuth
                                    tokens and notebook caches are left alone)
"""
import argparse
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


def remove_existing(target: Path):
    """ Remove a file or (possibly dangling) symlink; return True if something was removed. """
    if target.is_symlink() or target.exists():
        target.unlink()
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Install googletasks2zim into Zim's plugin folder.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--link", action="store_true", help="symlink the files instead of copying them")
    mode.add_argument("--uninstall", action="store_true", help="remove the installed plugin files")
    args = parser.parse_args()

    dest = zim_plugins_dir()

    if args.uninstall:
        for src in FILES_TO_COPY:
            if remove_existing(dest / src.name):
                print(f"Removed {dest / src.name}")
            else:
                print(f"Not installed: {dest / src.name}")
        print("\nOAuth tokens (googletasks_oauth*.json) in the plugins folder and pip dependencies were kept.")
        return

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REPO_ROOT / "requirements.txt"), "--break-system-packages"],
        check=True,
    )

    dest.mkdir(parents=True, exist_ok=True)
    for src in FILES_TO_COPY:
        target = dest / src.name
        remove_existing(target)  # never write through an old symlink into the repo file
        if args.link:
            target.symlink_to(src)
            print(f"Linked {target} -> {src}")
        else:
            shutil.copy2(src, target)
            print(f"Copied {src.name} -> {target}")

    print(
        "\nDone. Now in Zim: enable 'Google tasks' in Edit / Preferences / Plugins, "
        "then configure it in File / Properties / Google Tasks."
    )


if __name__ == "__main__":
    main()
