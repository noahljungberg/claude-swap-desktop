"""Account switching for the Claude desktop app (macOS).

The app keeps its login in two places inside its userData dir:
- ``Cookies`` (sqlite) — the claude.ai ``sessionKey`` the main view uses
- ``config.json`` — ``oauth:tokenCache*`` (Claude Code token) + ``lastKnownAccountUuid``

Both are encrypted with Electron safeStorage, whose key is per-machine
("Claude Safe Storage" in the login keychain), so snapshots are copied as
ciphertext and never decrypted.

Local Code sessions live in ``claude-code-sessions/<accountUuid>/<orgUuid>/``
as ``local_*.json`` index files (transcripts are in ~/.claude/projects, which is
already shared). The app refuses symlinked/multi-linked files there, so on every
save/switch we copy the newest version of each session file into the active
account's org dirs. Cloud sessions are server-side and stay per account.

Everything here edits files the app rewrites while running, so each command
quits the app first and relaunches it after.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from claude_swap.paths import get_backup_root

APP_NAME = "Claude"
USER_DATA = Path.home() / "Library" / "Application Support" / "Claude"
AUTH_KEYS = ("oauth:tokenCache", "oauth:tokenCacheV2", "lastKnownAccountUuid")
SESSIONS_DIR = "claude-code-sessions"
# session index files we sync; scheduled-tasks.json etc. stay per account
SESSION_PREFIXES = ("local_", "deleted_")


def snapshots_root() -> Path:
    return get_backup_root() / "desktop"


# --- app lifecycle ---------------------------------------------------------

def app_running() -> bool:
    return subprocess.run(["pgrep", "-x", APP_NAME], capture_output=True).returncode == 0


def quit_app(timeout: float = 30) -> None:
    if not app_running():
        return
    subprocess.run(["osascript", "-e", f'quit app "{APP_NAME}"'], capture_output=True)
    deadline = time.monotonic() + timeout
    while app_running():
        if time.monotonic() > deadline:
            raise RuntimeError(f"{APP_NAME} did not quit within {timeout:.0f}s (a confirm dialog open?)")
        time.sleep(0.5)


def launch_app() -> None:
    subprocess.run(["open", "-a", APP_NAME], check=False)


# --- config.json -----------------------------------------------------------

def read_config(user_data: Path) -> dict:
    return json.loads((user_data / "config.json").read_text())


def write_config(user_data: Path, cfg: dict) -> None:
    path = user_data / "config.json"
    tmp = path.with_suffix(".json.cswap-tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def current_account(user_data: Path) -> str | None:
    cfg = read_config(user_data)
    # logged out = no token cache, even if lastKnownAccountUuid lingers
    return cfg.get("lastKnownAccountUuid") if cfg.get("oauth:tokenCacheV2") else None


# --- snapshots -------------------------------------------------------------

def save_snapshot(user_data: Path, root: Path, label: str | None = None) -> str:
    account = current_account(user_data)
    if not account:
        raise RuntimeError("The desktop app is not logged in")
    cfg = read_config(user_data)
    dest = root / account
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    meta_path = dest / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if label:
        meta["label"] = label
    meta["auth"] = {k: cfg[k] for k in AUTH_KEYS if k in cfg}
    meta_path.write_text(json.dumps(meta, indent=2))
    os.chmod(meta_path, 0o600)
    shutil.copy2(user_data / "Cookies", dest / "Cookies")
    return account


def restore_snapshot(user_data: Path, root: Path, account: str) -> None:
    src = root / account
    meta = json.loads((src / "meta.json").read_text())
    cfg = read_config(user_data)
    for k in AUTH_KEYS:
        cfg.pop(k, None)
    cfg.update(meta["auth"])
    write_config(user_data, cfg)
    (user_data / "Cookies-journal").unlink(missing_ok=True)
    shutil.copy2(src / "Cookies", user_data / "Cookies")


def clear_login(user_data: Path) -> None:
    cfg = read_config(user_data)
    for k in AUTH_KEYS:
        cfg.pop(k, None)
    write_config(user_data, cfg)
    for name in ("Cookies", "Cookies-journal"):
        (user_data / name).unlink(missing_ok=True)


def list_snapshots(root: Path) -> list[tuple[str, str]]:
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if (d / "meta.json").exists():
            out.append((d.name, json.loads((d / "meta.json").read_text()).get("label", "")))
    return out


def resolve(root: Path, query: str) -> str:
    snaps = list_snapshots(root)
    hits = [a for a, label in snaps if label == query] or [a for a, _ in snaps if a.startswith(query)]
    if len(hits) != 1:
        raise RuntimeError(f"No unique desktop account matches {query!r}")
    return hits[0]


# --- local session sync ----------------------------------------------------

def sync_sessions(user_data: Path, account: str) -> int:
    """Copy the newest version of every local session file into ``account``'s org dirs."""
    base = user_data / SESSIONS_DIR
    targets = [d for d in (base / account).glob("*") if d.is_dir()]
    if not targets:
        return 0  # app hasn't opened this account yet; next save/switch picks it up
    newest: dict[str, Path] = {}
    for f in base.glob("*/*/*"):
        if f.is_file() and f.name.startswith(SESSION_PREFIXES):
            cur = newest.get(f.name)
            if cur is None or f.stat().st_mtime > cur.stat().st_mtime:
                newest[f.name] = f
    copied = 0
    for target in targets:
        for name, src in newest.items():
            dst = target / name
            if src.parent == target or (dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime):
                continue
            shutil.copy2(src, dst)
            copied += 1
        if copied:
            # just a load hint; the app rebuilds it from isArchived in the json
            (target / "archived-sessions.idx").unlink(missing_ok=True)
    return copied


# --- commands --------------------------------------------------------------

def _with_app_closed(fn):
    was_running = app_running()
    quit_app()
    try:
        return fn()
    finally:
        if was_running:
            launch_app()


def cmd_save(label: str | None) -> None:
    def go():
        account = save_snapshot(USER_DATA, snapshots_root(), label)
        n = sync_sessions(USER_DATA, account)
        print(f"Saved desktop account {account}{f' ({label})' if label else ''}; synced {n} session files")
    _with_app_closed(go)


def cmd_switch(query: str) -> None:
    root = snapshots_root()
    target = resolve(root, query)
    quit_app()
    # re-save the outgoing login first so its refreshed tokens aren't lost
    if current_account(USER_DATA):
        save_snapshot(USER_DATA, root)
    restore_snapshot(USER_DATA, root, target)
    n = sync_sessions(USER_DATA, target)
    launch_app()
    print(f"Switched desktop app to {target}; synced {n} session files")


def cmd_login() -> None:
    quit_app()
    if current_account(USER_DATA):
        save_snapshot(USER_DATA, snapshots_root())
    clear_login(USER_DATA)
    launch_app()
    print("Log in with the other account in the app, then run: cswap desktop save <label>")


def cmd_list() -> None:
    active = current_account(USER_DATA)
    snaps = list_snapshots(snapshots_root())
    if not snaps:
        print("No desktop accounts saved. Run: cswap desktop save <label>")
    for account, label in snaps:
        print(f"{'*' if account == active else ' '} {account}  {label}")


USAGE = """usage: cswap desktop <command>
  list                 saved desktop-app accounts (* = active)
  save [label]         snapshot the app's current login
  login                park current login, reopen the app logged out to add another
  switch <label|uuid>  switch the desktop app to a saved account

Quits and relaunches the Claude app, so run it from a regular terminal,
not from a session inside the app."""


def main(argv: list[str]) -> None:
    if sys.platform != "darwin":
        sys.exit("cswap desktop is macOS-only")
    try:
        match argv:
            case [] | ["list"]:
                cmd_list()
            case ["save"]:
                cmd_save(None)
            case ["save", label]:
                cmd_save(label)
            case ["login"]:
                cmd_login()
            case ["switch", query]:
                cmd_switch(query)
            case _:
                sys.exit(USAGE)
    except RuntimeError as e:
        sys.exit(f"error: {e}")
