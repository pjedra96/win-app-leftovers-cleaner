"""Clean up leftovers that apps keep around after updating.

Squirrel apps: apps installed with Squirrel.Windows (GitHub Desktop, MongoDB
Compass, Discord, Slack, Postman, ...) live in %LOCALAPPDATA%\\<App>\\ with an
Update.exe next to one app-<version> folder per installed version, plus .nupkg
files in packages\\. Squirrel is meant to delete old versions after updating but
often leaves them. The newest app-* folder and that version's packages are kept.

VS Code: %APPDATA%\\Code\\CachedData holds one folder of precompiled code per VS
Code build. Only the current build's folder is ever read again; older ones are
offered once they haven't been used since before yesterday. %APPDATA%\\Code\\
CachedExtensionVSIXs holds spare copies of downloaded extension packages and
unfinished downloads; files from before yesterday are offered, and are only
deleted while VS Code is closed. Crash dumps in %APPDATA%\\Code\\Crashpad\\reports
are listed unticked, since keeping them is the user's call.

npm and pip: their download caches, cleared with the tools' own commands.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

LOCAL_APPDATA = Path(os.environ["LOCALAPPDATA"])
ROAMING_APPDATA = Path(os.environ["APPDATA"])
ALLOWED_BASES = (LOCAL_APPDATA, ROAMING_APPDATA)
VSCODE_CACHED_DATA = ROAMING_APPDATA / "Code" / "CachedData"
VSCODE_VSIX_CACHE = ROAMING_APPDATA / "Code" / "CachedExtensionVSIXs"
VSCODE_CRASH_REPORTS = ROAMING_APPDATA / "Code" / "Crashpad" / "reports"
VSCODE_EXE = "code.exe"
VSCODE_INSTALLS = (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft VS Code",
                   LOCAL_APPDATA / "Programs" / "Microsoft VS Code")
PACKAGE_RE = re.compile(r"-(\d+(?:\.\d+)+)(.*)\.nupkg$", re.IGNORECASE)
CHECKED = "\u2611"
UNCHECKED = "\u2610"


@dataclass
class Candidate:
    path: Path
    kind: str  # "folder", "package", "file" or "command"
    label: str
    size: int = 0
    note: str = ""
    container: Path = None  # folder the item must sit directly in to be deleted
    command: list = None  # for "command" items: what to run instead of deleting
    close_first: tuple = None  # (exe name, app name) that must not be running
    checked: bool = True


@dataclass
class Group:
    name: str
    root: Path
    note: str  # shown next to the name, e.g. "current: 1.53.0"
    candidates: list = field(default_factory=list)


def version_key(text):
    return tuple(int(n) for n in re.findall(r"\d+", text))


def dir_size(path):
    total = 0
    for folder, dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


def last_used(path):
    """Newest modification time of a folder or anything inside it."""
    newest = path.stat().st_mtime
    for folder, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(folder, name)))
            except OSError:
                pass
    return datetime.fromtimestamp(newest)


def start_of_yesterday():
    return datetime.combine(date.today() - timedelta(days=1), time.min)


def format_size(size):
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def subfolders(path):
    try:
        return [d for d in path.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError:
        return []


def run_quietly(command, timeout=120):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def squirrel_app_folders():
    """Folders holding a Squirrel Update.exe, at %LOCALAPPDATA%\\<App> or one level
    deeper under a vendor folder, like classic Teams at %LOCALAPPDATA%\\Microsoft\\Teams."""
    found = []
    for top in subfolders(LOCAL_APPDATA):
        for folder in [top] + subfolders(top):
            try:
                if (folder / "Update.exe").is_file():
                    found.append(folder)
            except OSError:
                pass
    return sorted(found, key=lambda p: str(p).lower())


def scan_squirrel():
    groups = []
    for app_dir in squirrel_app_folders():
        versions = [d for d in subfolders(app_dir) if d.name.lower().startswith("app-")]
        if not versions:
            continue

        # Squirrel's launcher stub always runs the highest app-* folder, so that is the live one.
        versions.sort(key=lambda d: version_key(d.name), reverse=True)
        current = versions[0]
        current_key = version_key(current.name)
        group = Group(str(app_dir.relative_to(LOCAL_APPDATA)), app_dir, f"current: {current.name[4:]}")

        for index, folder in enumerate(versions[1:]):
            # Squirrel itself keeps the one before current so it can roll back.
            note = "previous version, Squirrel's rollback copy" if index == 0 else ""
            group.candidates.append(Candidate(
                folder, "folder", folder.name, dir_size(folder), note, container=app_dir))

        # Only packages older than the current version go; the current full package is
        # what Squirrel applies future delta updates against.
        packages = app_dir / "packages"
        if packages.is_dir():
            old_packages = []
            for package in packages.glob("*.nupkg"):
                match = PACKAGE_RE.search(package.name)
                if match and version_key(match[1]) < current_key:
                    old_packages.append((version_key(match[1]), package))
            for key, package in sorted(old_packages):
                group.candidates.append(Candidate(
                    package, "package", package.name, package.stat().st_size, container=packages))

        if group.candidates:
            groups.append(group)
    return groups


def vscode_commits():
    """Commit hashes of the installed VS Code builds, read from their product.json."""
    commits = set()
    for install in VSCODE_INSTALLS:
        # Newer builds keep their files in a <short-commit>\ subfolder of the install folder.
        for product in [install / "resources" / "app" / "product.json",
                        *install.glob("*/resources/app/product.json")]:
            try:
                commits.add(json.loads(product.read_text(encoding="utf-8"))["commit"])
            except (OSError, ValueError, KeyError):
                pass
    return commits


def scan_vscode():
    builds = subfolders(VSCODE_CACHED_DATA)
    if not builds:
        return []
    commits = vscode_commits()
    used = {build: last_used(build) for build in builds}
    newest = max(builds, key=used.get)
    cutoff = start_of_yesterday()

    if commits:
        note = "current build: " + ", ".join(sorted(c[:10] for c in commits))
    else:
        note = "current build unknown, keeping the most recently used"
    group = Group("VS Code CachedData", VSCODE_CACHED_DATA, note)
    for build in sorted(builds, key=used.get):
        if build.name in commits or build == newest or used[build] >= cutoff:
            continue
        group.candidates.append(Candidate(
            build, "folder", build.name[:10], dir_size(build),
            f"last used {used[build]:%Y-%m-%d}", container=VSCODE_CACHED_DATA))
    return [group] if group.candidates else []


def scan_vscode_vsix():
    try:
        files = [f for f in VSCODE_VSIX_CACHE.iterdir() if f.is_file()]
    except OSError:
        return []
    cutoff = start_of_yesterday().timestamp()
    group = Group("VS Code CachedExtensionVSIXs", VSCODE_VSIX_CACHE,
                  "spare extension packages, VS Code must be closed to delete")
    for package in sorted(files, key=lambda f: f.stat().st_mtime):
        info = package.stat()
        if info.st_mtime >= cutoff:
            continue
        # Downloads go to a .<guid> temp name first; ones still named that way never finished.
        note = "unfinished download" if package.name.startswith(".") else "spare copy"
        note += f", {datetime.fromtimestamp(info.st_mtime):%Y-%m-%d}"
        group.candidates.append(Candidate(
            package, "file", package.name, info.st_size, note,
            container=VSCODE_VSIX_CACHE, close_first=(VSCODE_EXE, "VS Code")))
    return [group] if group.candidates else []


def scan_vscode_crashes():
    try:
        dumps = [f for f in VSCODE_CRASH_REPORTS.iterdir() if f.is_file()]
    except OSError:
        return []
    group = Group("VS Code crash reports", VSCODE_CRASH_REPORTS,
                  "only needed to investigate past crashes, unticked: your call")
    for dump in sorted(dumps, key=lambda f: f.stat().st_mtime):
        info = dump.stat()
        # Unticked by default: unlike caches, these can't be recreated once gone.
        group.candidates.append(Candidate(
            dump, "file", dump.name, info.st_size,
            f"crash on {datetime.fromtimestamp(info.st_mtime):%Y-%m-%d}",
            container=VSCODE_CRASH_REPORTS, checked=False))
    return [group] if group.candidates else []


def scan_npm():
    npm = shutil.which("npm")
    if not npm:
        return []
    try:
        cache = Path(run_quietly([npm, "config", "get", "cache"], timeout=30).stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return []
    # npm cache clean only empties _cacache; _npx and _logs are left alone.
    cacache = cache / "_cacache"
    size = dir_size(cacache) if cacache.is_dir() else 0
    if not size:
        return []
    group = Group("npm cache", cache, "downloaded packages, fetched again when needed")
    group.candidates.append(Candidate(
        cacache, "command", "npm cache clean --force", size, command=[npm, "cache", "clean", "--force"]))
    return [group]


def scan_pip():
    pip = [sys.executable, "-m", "pip"]
    try:
        result = run_quietly(pip + ["cache", "dir"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    cache = Path(result.stdout.strip()) if result.returncode == 0 else None
    size = dir_size(cache) if cache and cache.is_dir() else 0
    if not size:
        return []
    group = Group("pip cache", cache, "downloaded packages, fetched again when needed")
    group.candidates.append(Candidate(
        cache, "command", "pip cache purge", size, command=pip + ["cache", "purge"]))
    return [group]


def scan():
    """Every group that has something to clean."""
    return (scan_squirrel() + scan_vscode() + scan_vscode_vsix() + scan_vscode_crashes()
            + scan_npm() + scan_pip())


def running_executables():
    """Lower-cased paths of every running process's executable."""
    command = ["powershell", "-NoProfile", "-Command",
               "Get-CimInstance Win32_Process | ForEach-Object { $_.ExecutablePath }"]
    try:
        result = run_quietly(command, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]


def clear_readonly_and_retry(func, path, exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove(candidate):
    if candidate.kind == "command":
        result = run_quietly(candidate.command)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"exit code {result.returncode}")
        return

    path = candidate.path
    # Refuse anything that isn't directly inside the folder the scan found it in,
    # somewhere under LOCALAPPDATA or APPDATA.
    if (candidate.container is None or path.parent != candidate.container
            or not any(base in path.parents for base in ALLOWED_BASES)):
        raise RuntimeError(f"Refusing to delete unexpected path {path}")

    if candidate.kind in ("package", "file"):
        path.unlink()
        return
    # Renaming a folder fails if any file inside is in use, so a locked folder is
    # left untouched instead of half-deleted.
    doomed = path.with_name(path.name + ".removing")
    path.rename(doomed)
    shutil.rmtree(doomed, onexc=clear_readonly_and_retry)


class CleanerWindow:
    def __init__(self, root):
        self.root = root
        self.groups = []
        self.rows = {}  # tree item id -> Candidate
        self.busy = False

        root.title("App Leftovers Cleaner")
        root.geometry("1060x560")
        root.minsize(560, 300)

        top = ttk.Frame(root, padding=(10, 10, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Old Squirrel app versions, old VS Code caches, and npm/pip caches. "
                            "Current versions are always kept.").pack(anchor="w")

        frame = ttk.Frame(root, padding=10)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("kind", "size", "path"), selectmode="none")
        self.tree.heading("#0", text="App / item")
        self.tree.heading("kind", text="Type")
        self.tree.heading("size", text="Size")
        self.tree.heading("path", text="Path")
        self.tree.column("#0", width=460)
        self.tree.column("kind", width=75, stretch=False)
        self.tree.column("size", width=130, stretch=False, anchor="e")
        self.tree.column("path", width=300)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.on_click)

        # Section headings: bold on a tinted band, so each app stands out from its items.
        ttk.Style(root).configure("Treeview", rowheight=24)
        self.bold_font = tkfont.nametofont("TkDefaultFont").copy()
        self.bold_font.configure(weight="bold")
        self.tree.tag_configure("group", font=self.bold_font, background="#dce6f5")
        self.tree.tag_configure("unchecked", foreground="#8a8a8a")

        bottom = ttk.Frame(root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        self.status = ttk.Label(bottom, text="")
        self.status.pack(side="left")
        self.delete_button = ttk.Button(bottom, text="Delete selected", command=self.on_delete)
        self.delete_button.pack(side="right")
        ttk.Button(bottom, text="Select none", command=lambda: self.set_all(False)).pack(side="right", padx=4)
        ttk.Button(bottom, text="Select all", command=lambda: self.set_all(True)).pack(side="right")
        self.rescan_button = ttk.Button(bottom, text="Rescan", command=self.start_scan)
        self.rescan_button.pack(side="right", padx=4)

        self.start_scan()

    def run_in_background(self, work, done, message):
        """Run work() off the UI thread, then call done(result) on it."""
        self.busy = True
        self.delete_button.state(["disabled"])
        self.rescan_button.state(["disabled"])
        self.status.config(text=message)
        outcome = {}

        def worker():
            try:
                outcome["result"] = work()
            except Exception as error:
                outcome["error"] = error

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        def poll():
            if thread.is_alive():
                self.root.after(100, poll)
                return
            self.busy = False
            self.delete_button.state(["!disabled"])
            self.rescan_button.state(["!disabled"])
            if "error" in outcome:
                messagebox.showerror("Error", str(outcome["error"]))
                self.update_status()
            else:
                done(outcome["result"])

        poll()

    def start_scan(self):
        self.run_in_background(scan, self.show_results, "Scanning...")

    def show_results(self, groups):
        self.groups = groups
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        for group in groups:
            parent = self.tree.insert("", "end", open=True, tags=("group",),
                                      text=f"{group.name}  \u2014 {group.note}",
                                      values=("", "", str(group.root)))
            for candidate in group.candidates:
                item = self.tree.insert(parent, "end", values=(
                    candidate.kind, format_size(candidate.size), str(candidate.path)))
                self.rows[item] = candidate
                self.refresh_row(item)
        if not groups:
            self.tree.insert("", "end", text="Nothing to clean up")
        self.update_status()

    def refresh_row(self, item):
        candidate = self.rows[item]
        mark = CHECKED if candidate.checked else UNCHECKED
        label = candidate.label
        if candidate.note:
            label += f"  ({candidate.note})"
        self.tree.item(item, text=f"{mark}  {label}", tags=() if candidate.checked else ("unchecked",))
        self.refresh_group(self.tree.parent(item))

    def refresh_group(self, parent):
        """Show how much the section frees with its current ticks, and the total when that differs."""
        candidates = [self.rows[item] for item in self.tree.get_children(parent)]
        total = sum(c.size for c in candidates)
        ticked = sum(c.size for c in candidates if c.checked)
        size = format_size(ticked) if ticked == total else f"{format_size(ticked)} of {format_size(total)}"
        self.tree.set(parent, "size", size)

    def on_click(self, event):
        item = self.tree.identify_row(event.y)
        if self.busy or item not in self.rows:
            return
        self.rows[item].checked = not self.rows[item].checked
        self.refresh_row(item)
        self.update_status()

    def set_all(self, checked):
        if self.busy:
            return
        for item, candidate in self.rows.items():
            candidate.checked = checked
            self.refresh_row(item)
        self.update_status()

    def selected(self):
        return [c for c in self.rows.values() if c.checked]

    def update_status(self):
        chosen = self.selected()
        self.status.config(
            text=f"{len(chosen)} item(s) selected, {format_size(sum(c.size for c in chosen))} to free")

    def on_delete(self):
        chosen = self.selected()
        if not chosen:
            return

        running = running_executables()
        running_names = {Path(exe).name for exe in running}
        must_close = sorted({c.close_first[1] for c in chosen
                             if c.close_first and c.close_first[0] in running_names})
        if must_close:
            messagebox.showwarning(
                "Programs still running",
                "Close these programs first, or untick their items:\n\n" + "\n".join(must_close))
            return

        in_use = sorted({str(c.path) for c in chosen if c.kind == "folder"
                         and any(exe.startswith(str(c.path).lower() + os.sep) for exe in running)})
        if in_use:
            messagebox.showwarning(
                "Programs still running",
                "These folders have programs running from them. Close those programs and try again:\n\n"
                + "\n".join(in_use))
            return

        size = format_size(sum(c.size for c in chosen))
        commands = [c.label for c in chosen if c.kind == "command"]
        message = f"Permanently delete {len(chosen)} item(s) ({size})?\n\nThis does not use the Recycle Bin."
        if commands:
            message += "\n\nCaches are cleared by running:\n" + "\n".join(commands)
        if not messagebox.askyesno("Confirm delete", message):
            return

        def work():
            failures = []
            for candidate in chosen:
                try:
                    remove(candidate)
                except Exception as error:
                    failures.append(f"{candidate.path}\n    {error}")
            return failures

        def done(failures):
            if failures:
                messagebox.showwarning(
                    "Some items were not deleted",
                    "Close the app if it is open, then rescan and try again.\n\n" + "\n".join(failures))
            self.start_scan()

        self.run_in_background(work, done, "Deleting...")


def main():
    root = tk.Tk()
    CleanerWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
