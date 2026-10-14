import json
import os

from claude_swap import desktop


def _app(tmp_path, account, token):
    ud = tmp_path / "ud"
    ud.mkdir(exist_ok=True)
    (ud / "config.json").write_text(json.dumps(
        {"scale": 1, "oauth:tokenCacheV2": token, "lastKnownAccountUuid": account}))
    (ud / "Cookies").write_text(f"cookies-{account}")
    return ud


def test_snapshot_roundtrip_keeps_other_config(tmp_path):
    root = tmp_path / "snaps"
    ud = _app(tmp_path, "acct-a", "tok-a")
    desktop.save_snapshot(ud, root, "work")
    _app(tmp_path, "acct-b", "tok-b")
    desktop.save_snapshot(ud, root, "personal")

    desktop.restore_snapshot(ud, root, desktop.resolve(root, "work"))
    cfg = desktop.read_config(ud)
    assert cfg == {"scale": 1, "oauth:tokenCacheV2": "tok-a", "lastKnownAccountUuid": "acct-a"}
    assert (ud / "Cookies").read_text() == "cookies-acct-a"
    assert desktop.list_snapshots(root) == [("acct-a", "work"), ("acct-b", "personal")]

    desktop.clear_login(ud)
    assert desktop.current_account(ud) is None and not (ud / "Cookies").exists()


def test_sync_sessions_newest_wins_and_skips_other_files(tmp_path):
    ud = tmp_path / "ud"
    a = ud / "claude-code-sessions" / "acct-a" / "org-1"
    b = ud / "claude-code-sessions" / "acct-b" / "org-2"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "local_1.json").write_text("new")
    (b / "local_1.json").write_text("old")
    os.utime(b / "local_1.json", (1, 1))
    (a / "deleted_2").write_text("x")
    (a / "scheduled-tasks.json").write_text("a-only")
    (b / "archived-sessions.idx").write_text("stale")

    assert desktop.sync_sessions(ud, "acct-b") == 2
    assert (b / "local_1.json").read_text() == "new"
    assert (b / "deleted_2").exists()
    assert not (b / "scheduled-tasks.json").exists()
    assert not (b / "archived-sessions.idx").exists()
    assert desktop.sync_sessions(ud, "acct-b") == 0
    assert desktop.sync_sessions(ud, "acct-new") == 0


def test_retire_ssh_processes_only_flips_adoptable(tmp_path):
    org = tmp_path / "claude-code-sessions" / "acct-a" / "org-1"
    org.mkdir(parents=True)
    (org / "local_ssh.json").write_text(json.dumps({"sshReattach": {"state": "adoptable", "processId": "p"}}))
    (org / "local_gone.json").write_text(json.dumps({"sshReattach": {"state": "blocked"}}))
    (org / "local_plain.json").write_text(json.dumps({"title": "local"}))

    assert desktop.retire_ssh_processes(tmp_path) == 1
    assert json.loads((org / "local_ssh.json").read_text())["sshReattach"] == {"state": "reap_only", "processId": "p"}
    assert json.loads((org / "local_gone.json").read_text())["sshReattach"]["state"] == "blocked"
    assert desktop.retire_ssh_processes(tmp_path) == 0
