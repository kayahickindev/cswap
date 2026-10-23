"""Tests for moving running Codex panes onto the active login."""

from __future__ import annotations

import json
from pathlib import Path

from claude_swap.codex_panes import STALL_RE, CodexPaneSync, codex_args, session_id
from tests.test_codex import PRO, SEAT_HELLO, SEAT_OPS, Harness


class FakeHerdr:
    """One Codex pane: agent list, process info, ps, screen, quit and run."""

    def __init__(self, home: Path, *, started="Thu Oct 22 21:45:17 2026", status="idle",
                 screen="›\n", name="sec-scanner"):
        self.home = home
        self.pid = 100
        self.started = started
        self.status = status
        self.screen = screen
        self.name = name
        self.calls: list[list[str]] = []
        self.running = True

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[0] == "env" and args[-2:] == ["daemon", "version"]:
            return json.dumps({"status": "running"}) if self.daemon else "{}"
        if args[0] == "env" and args[-2:] == ["daemon", "restart"]:
            self.daemon_started = "Fri Oct 23 23:59:59 2099"
            return "{}"
        if args[0] == "ps":
            if "-axo" in args:
                return "300 codex app-server --listen unix://\n" if self.daemon else ""
            if "lstart=" in args:
                return (self.daemon_started if args[-1] == "300" else self.started) + "\n"
            return f"codex CODEX_HOME={self.home} PATH=/usr/bin\n"
        verb = args[1:3]
        if verb == ["agent", "list"]:
            agents = [{"agent": "codex", "agent_status": self.status, "pane_id": "wD:p1",
                       "name": self.name, "cwd": "/repo"}] if self.running or self.restarted else []
            return json.dumps({"result": {"agents": agents}})
        if verb == ["pane", "process-info"]:
            procs = [{"name": "codex", "argv": ["codex", "--yolo"], "cwd": "/repo", "pid": self.pid}] \
                if self.running else []
            return json.dumps({"result": {"process_info": {"foreground_processes": procs}}})
        if verb == ["agent", "read"]:
            return self.screen
        if verb == ["agent", "prompt"] and args[4] == "/quit":
            self.running = False
            return "{}"
        if verb == ["pane", "run"]:
            self.running = True
            self.pid = 200
            self.status = "working"
            self.restarted = True
            return "{}"
        return "{}"

    restarted = False
    daemon = False
    daemon_started = "Thu Oct 22 16:35:57 2026"


def _sync(h: Harness, fake: FakeHerdr, *, now=None, sid="thread-1"):
    clock = [now or 1_900_000_000.0]
    sync = CodexPaneSync(
        h.store, "herdr", run=fake, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        find_session=lambda home, cwd, since: sid,
    )
    return sync, clock


def _rotated(h, tmp_path):
    """Daybreak home rotating between two seats, switched once after the pane started."""
    h.write_live(PRO)
    h.store.adopt_live()
    daybreak = tmp_path / "db"
    daybreak.mkdir()
    (daybreak / "auth.json").write_text(json.dumps(json.loads(
        __import__("tests.test_codex", fromlist=["_auth"])._auth(*SEAT_OPS, refresh="rt-db"))))
    h.store.set_live_home(daybreak)
    h.logins.append(SEAT_HELLO)
    h.store.add_login()
    return daybreak


def test_pane_started_on_the_old_login_restarts_into_its_conversation(tmp_path):
    h = Harness(tmp_path)
    daybreak = _rotated(h, tmp_path)
    fake = FakeHerdr(daybreak)
    sync, clock = _sync(h, fake)
    h.store.switch("3")
    assert sync.sync(min_idle_s=0) == [
        {"pane": "wD:p1", "name": "sec-scanner", "session": "thread-1", "continued": False}
    ]
    run = next(c for c in fake.calls if c[1:3] == ["pane", "run"])
    assert run[4] == f"env CODEX_HOME={daybreak} codex --yolo resume thread-1"
    assert ["herdr", "agent", "rename", "wD:p1", "sec-scanner"] in fake.calls


def test_a_stalled_pane_restarts_at_once_and_continues(tmp_path):
    h = Harness(tmp_path)
    daybreak = _rotated(h, tmp_path)
    fake = FakeHerdr(daybreak, screen="■ You've hit your usage limit. Visit https://chatgpt.com/codex\n›")
    sync, _ = _sync(h, fake)
    h.store.switch("3")
    result = sync.sync()  # default idle wait does not apply to a stall
    assert result and result[0]["continued"]
    run = next(c for c in fake.calls if c[1:3] == ["pane", "run"])
    assert run[4].endswith("resume thread-1 continue")


def test_an_idle_pane_waits_before_restarting(tmp_path):
    h = Harness(tmp_path)
    daybreak = _rotated(h, tmp_path)
    fake = FakeHerdr(daybreak)
    sync, clock = _sync(h, fake)
    h.store.switch("3")
    assert sync.sync(min_idle_s=90) == []
    clock[0] += 100
    assert len(sync.sync(min_idle_s=90)) == 1


def test_busy_and_current_panes_are_left_alone(tmp_path):
    h = Harness(tmp_path)
    daybreak = _rotated(h, tmp_path)
    busy = FakeHerdr(daybreak, status="working")
    sync, _ = _sync(h, busy)
    h.store.switch("3")
    assert sync.sync(min_idle_s=0) == []
    # A pane that started after the switch is already on the active login.
    fresh = FakeHerdr(daybreak, started="Fri Oct 23 23:59:59 2099")
    sync, _ = _sync(h, fresh)
    assert sync.sync(min_idle_s=0) == []
    assert not any(c[1:3] == ["pane", "run"] for c in fresh.calls)


def test_panes_on_another_home_are_never_touched(tmp_path):
    h = Harness(tmp_path)
    _rotated(h, tmp_path)
    pro_pane = FakeHerdr(h.live)  # plain codex on the pinned Pro home
    sync, _ = _sync(h, pro_pane)
    h.store.switch("3")
    assert sync.sync(min_idle_s=0) == []


def test_a_stall_with_no_known_conversation_is_left_for_a_person(tmp_path):
    h = Harness(tmp_path)
    daybreak = _rotated(h, tmp_path)
    fake = FakeHerdr(daybreak, screen="You've hit your usage limit.")
    sync, _ = _sync(h, fake, sid=None)
    h.store.switch("3")
    result = sync.sync()
    assert result and "error" in result[0]
    assert not any(c[1:3] == ["agent", "prompt"] for c in fake.calls)


def test_codex_args_drop_an_earlier_resume():
    assert codex_args(["codex", "--yolo", "resume", "abc", "continue"]) == ["--yolo"]
    assert codex_args(["codex"]) == []


def test_session_id_picks_the_newest_rollout_for_the_folder(tmp_path):
    day = tmp_path / "sessions" / "2026" / "10" / "23"
    day.mkdir(parents=True)
    for name, cwd, sid in (("rollout-a.jsonl", "/repo", "old"), ("rollout-b.jsonl", "/elsewhere", "x"),
                           ("rollout-c.jsonl", "/repo", "new")):
        (day / name).write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}) + "\n")
    import os
    os.utime(day / "rollout-a.jsonl", (1000, 1000))
    os.utime(day / "rollout-c.jsonl", (3000, 3000))
    os.utime(day / "rollout-b.jsonl", (4000, 4000))
    assert session_id(tmp_path, "/repo", since=500) == "new"
    assert session_id(tmp_path, "/repo", since=3500) is None


def test_stall_text_matches_codex_wording():
    assert STALL_RE.search("You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage")
    assert STALL_RE.search("Usage limit reached. You've reached your usage limit.")
    assert not STALL_RE.search("Reach for it for any job; usage limits go even further")


def test_a_daemon_on_the_old_login_waits_for_quiet_then_restarts_with_its_panes(tmp_path):
    h = Harness(tmp_path)
    daybreak = _rotated(h, tmp_path)
    fake = FakeHerdr(daybreak, status="working")
    fake.daemon = True
    sync, clock = _sync(h, fake)
    h.store.switch("3")
    assert sync.sync(min_idle_s=60) == []  # a turn is running
    assert not any(c[-1] == "restart" for c in fake.calls)
    fake.status = "idle"
    assert sync.sync(min_idle_s=60) == []  # quiet, but not for long enough
    clock[0] += 61
    done = sync.sync(min_idle_s=60)
    assert done[0] == {"daemon": str(daybreak), "restarted": True}
    assert done[1]["pane"] == "wD:p1" and not done[1]["continued"]
    order = [c for c in fake.calls if c[-1] == "restart" or c[1:3] == ["pane", "run"]]
    assert order[0][-1] == "restart" and order[1][1:3] == ["pane", "run"]


def test_a_stall_behind_a_stale_daemon_restarts_at_once(tmp_path):
    h = Harness(tmp_path)
    daybreak = _rotated(h, tmp_path)
    fake = FakeHerdr(daybreak, screen="You've hit your usage limit. Visit https://chatgpt.com/codex")
    fake.daemon = True
    sync, _ = _sync(h, fake)
    h.store.switch("3")
    done = sync.sync()
    assert done[0]["restarted"] and done[1]["continued"]


def test_a_daemon_started_on_the_active_login_is_left_running(tmp_path):
    h = Harness(tmp_path)
    daybreak = _rotated(h, tmp_path)
    fake = FakeHerdr(daybreak, started="Fri Oct 23 23:59:59 2099")
    fake.daemon = True
    fake.daemon_started = "Fri Oct 23 23:59:59 2099"
    sync, _ = _sync(h, fake)
    h.store.switch("3")
    assert sync.sync(min_idle_s=0) == []
    assert not any(c[-1] == "restart" for c in fake.calls)
