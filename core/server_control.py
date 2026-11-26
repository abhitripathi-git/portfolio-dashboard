"""Process management helpers for restarting the Streamlit server from the UI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from config import BASE_DIR, DATA_DIR


def _detect_port(default_port: int = 8501) -> int:
    """Detect `--server.port` from sys.argv if passed, else return `default_port`."""
    args = sys.argv[1:]
    for idx, tok in enumerate(args):
        if tok.startswith("--server.port="):
            try:
                return int(tok.split("=", 1)[1])
            except ValueError:
                pass
        elif tok == "--server.port" and idx + 1 < len(args):
            try:
                return int(args[idx + 1])
            except ValueError:
                pass
    return default_port


def schedule_server_restart(
    port: int | None = None,
    delay_seconds: float = 0.6,
    app_path: Path | None = None,
    log_path: Path | None = None,
) -> Path:
    """Spawn a detached restarter that terminates this server and starts a fresh one.

    Returns the log file path used by the restarted server.
    """
    target_pid = os.getpid()
    target_port = int(port or _detect_port(8501))
    target_app = str(app_path or (BASE_DIR / "app.py"))
    target_log = Path(log_path or (DATA_DIR / "streamlit.log"))
    target_log.parent.mkdir(parents=True, exist_ok=True)
    python_exe = sys.executable

    helper_script = f"""
import os, signal, socket, subprocess, time

pid = {target_pid}
port = {target_port}
delay = {float(delay_seconds)}
python_exe = {python_exe!r}
app_path = {target_app!r}
cwd = {str(BASE_DIR)!r}
log_path = {str(target_log)!r}

time.sleep(delay)

try:
    os.kill(pid, signal.SIGTERM)
except ProcessLookupError:
    pass

deadline = time.monotonic() + 3.5
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
        time.sleep(0.1)
    except ProcessLookupError:
        break
else:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

port_deadline = time.monotonic() + 3.0
while time.monotonic() < port_deadline:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.1)

with open(log_path, "ab") as lf:
    subprocess.Popen(
        [
            python_exe, "-m", "streamlit", "run", app_path,
            "--server.port", str(port),
            "--server.headless", "true",
        ],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=lf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
"""

    with open(target_log, "ab") as lf:
        subprocess.Popen(
            [python_exe, "-c", helper_script],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return target_log


def _find_google_drive_backup_dir() -> Path | None:
    """Return a Google Drive sync folder on macOS if Google Drive for Desktop is mounted."""
    cloud_root = Path.home() / "Library" / "CloudStorage"
    if not cloud_root.exists():
        return None
    for entry in sorted(cloud_root.iterdir()):
        if entry.is_dir() and entry.name.startswith("GoogleDrive"):
            my_drive = entry / "My Drive"
            base = my_drive if my_drive.exists() else entry
            return base / "Portfolio-Dashboard-Backup"
    return None


def sync_backup_to_cloud(db_path: Path, backup_dir: Path | None = None) -> dict[str, object]:
    """Create an atomic SQLite snapshot + portable zip and sync to Google Drive (or local backups/)."""
    import shutil
    import sqlite3
    from datetime import datetime

    drive_dir = backup_dir or _find_google_drive_backup_dir()
    if drive_dir is not None:
        dest_dir = drive_dir
        mode = "Google Drive"
    else:
        dest_dir = BASE_DIR / "backups"
        mode = "Local backups/"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Atomic online SQLite backup (safe even with WAL active)
    db_dest = dest_dir / "portfolio.db"
    src_conn = sqlite3.connect(str(db_path))
    dst_conn = sqlite3.connect(str(db_dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    # 2. Run package_backup.sh if available and copy the latest zip
    pkg_script = BASE_DIR / "scripts" / "package_backup.sh"
    zip_dest: Path | None = None
    if pkg_script.exists():
        try:
            subprocess.run(["bash", str(pkg_script)], cwd=str(BASE_DIR), check=False, capture_output=True, timeout=45)
        except subprocess.TimeoutExpired:
            pass
        zips = sorted(BASE_DIR.parent.glob("portfolio-dashboard-backup-*.zip"))
        if zips:
            latest_zip = zips[-1]
            zip_dest = dest_dir / latest_zip.name
            shutil.copy2(latest_zip, zip_dest)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    return {
        "mode": mode,
        "dest_dir": dest_dir,
        "db_backup_path": db_dest,
        "zip_backup_path": zip_dest,
        "message": f"Synced to {mode} at {ts}",
    }


def sync_code_to_github(repo_dir: Path | None = None) -> dict[str, object]:
    """Stage, commit (if dirty), and push code changes to GitHub (`origin`), never touching DB/.env."""
    from datetime import datetime

    root = Path(repo_dir or BASE_DIR)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    if not (root / ".git").exists():
        return {"ok": False, "message": "Git repository not initialized in project root."}

    # Stage updates to already-tracked files only (never auto-adds stray untracked exports)
    add_res = subprocess.run(["git", "add", "-u"], cwd=str(root), env=env, capture_output=True, text=True)
    if add_res.returncode != 0:
        return {"ok": False, "message": f"git add failed: {add_res.stderr.strip()}"}

    status_res = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root), env=env, capture_output=True, text=True
    )
    committed = False
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    if status_res.stdout.strip():
        cmt_res = subprocess.run(
            ["git", "commit", "-m", f"Sync portfolio dashboard ({ts})"],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
        )
        if cmt_res.returncode != 0:
            return {"ok": False, "message": f"git commit failed: {cmt_res.stderr.strip() or cmt_res.stdout.strip()}"}
        committed = True

    try:
        push_res = subprocess.run(
            ["git", "push", "-u", "origin", "HEAD:main"],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "committed": committed, "message": "git push timed out after 45s."}
    if push_res.returncode != 0:
        err = (push_res.stderr or push_res.stdout or "").strip()
        return {"ok": False, "committed": committed, "message": f"git push failed: {err}"}

    note = "Committed & pushed" if committed else "Already up-to-date; pushed"
    return {"ok": True, "committed": committed, "message": f"{note} to GitHub at {ts}"}


def sync_online(db_path: Path, backup_dir: Path | None = None) -> dict[str, object]:
    """Single-click online sync: DB + zip to Google Drive, and code changes to GitHub."""
    from datetime import datetime

    drive_res = sync_backup_to_cloud(db_path, backup_dir=backup_dir)
    git_res = sync_code_to_github()
    ts = datetime.now().strftime("%H:%M")

    if git_res.get("ok"):
        return {
            "ok": True,
            "drive": drive_res,
            "github": git_res,
            "message": f"Synced {drive_res['mode']} + GitHub at {ts}",
        }
    return {
        "ok": False,
        "drive": drive_res,
        "github": git_res,
        "message": f"{drive_res['mode']} synced at {ts} · GitHub: {git_res.get('message', 'push skipped')}",
    }
