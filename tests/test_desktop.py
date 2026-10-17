"""Tests for the Claude desktop app follower (claude_swap.desktop)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from claude_swap.desktop import DesktopFollower, account_key, follow_active


class FakeApp:
    def __init__(self, running: bool = False, quits: bool = True):
        self.running = running
        self.quits = quits
        self.calls: list[str] = []

    def is_running(self) -> bool:
        return self.running

    def quit(self) -> bool:
        self.calls.append("quit")
        if self.quits:
            self.running = False
        return self.quits

    def launch(self) -> None:
        self.calls.append("launch")
        self.running = True


def _sign_in(data: Path, who: str) -> None:
    """Write the account-specific files the app keeps for one sign-in."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(who)
    (data / "Cookies").write_text(who)
    (data / "Local Storage").mkdir()
    (data / "Local Storage" / "leveldb").write_text(who)


def _setup(tmp_path: Path, app: FakeApp | None = None):
    data = tmp_path / "Claude"
    _sign_in(data, "a")
    (data / "vm_bundles").mkdir()
    (data / "vm_bundles" / "disk.img").write_text("shared")
    (data / "claude_desktop_config.json").write_text("mcp")
    follower = DesktopFollower(tmp_path / "backup", data_dir=data, app=app or FakeApp())
    return follower, data


def _connect_two(tmp_path: Path, app: FakeApp | None = None):
    follower, data = _setup(tmp_path, app)
    assert follower.connect("a@x.com", "org-a")["action"] == "connected"
    assert follower.new_login()["action"] == "signed-out"
    follower.app.running = False
    _sign_in(data, "b")
    assert follower.connect("b@x.com", "org-b")["action"] == "connected"
    return follower, data


def test_connect_needs_a_sign_in(tmp_path: Path):
    data = tmp_path / "Claude"
    data.mkdir()
    follower = DesktopFollower(tmp_path / "backup", data_dir=data, app=FakeApp())

    assert follower.connect("a@x.com", "org-a")["action"] == "skipped"
    assert not follower.is_active()


def test_new_login_parks_the_sign_in_and_keeps_shared_entries(tmp_path: Path):
    follower, data = _setup(tmp_path)
    follower.connect("a@x.com", "org-a")

    assert follower.new_login()["action"] == "signed-out"

    assert not (data / "config.json").exists()
    assert (data / "vm_bundles" / "disk.img").read_text() == "shared"
    assert (data / "claude_desktop_config.json").read_text() == "mcp"
    parked = follower.parked_dir(account_key("a@x.com", "org-a"))
    assert (parked / "config.json").read_text() == "a"
    assert follower.app.calls == ["launch"]


def test_follow_swaps_while_the_app_is_closed(tmp_path: Path):
    follower, data = _connect_two(tmp_path)

    result = follower.follow("A@x.com", "org-a", allow_restart=False)

    assert result["action"] == "switched"
    assert (data / "config.json").read_text() == "a"
    assert (data / "Local Storage" / "leveldb").read_text() == "a"
    parked_b = follower.parked_dir(account_key("b@x.com", "org-b"))
    assert (parked_b / "Cookies").read_text() == "b"
    assert not follower.parked_dir(account_key("a@x.com", "org-a")).exists()
    assert (data / "vm_bundles" / "disk.img").read_text() == "shared"
    assert follower.load()["current"] == account_key("a@x.com", "org-a")

    back = follower.follow("b@x.com", "org-b", allow_restart=False)
    assert back["action"] == "switched"
    assert (data / "config.json").read_text() == "b"


def test_auto_tick_never_restarts_a_running_app(tmp_path: Path):
    follower, data = _connect_two(tmp_path)
    follower.app.running = True
    follower.app.calls.clear()

    result = follower.follow("a@x.com", "org-a", allow_restart=False)

    assert result["action"] == "deferred"
    assert follower.app.calls == []
    assert (data / "config.json").read_text() == "b"
    assert follower.load()["pending"] == account_key("a@x.com", "org-a")

    follower.app.running = False
    assert follower.follow("a@x.com", "org-a", allow_restart=False)["action"] == "switched"
    assert (data / "config.json").read_text() == "a"
    assert follower.load()["pending"] is None


def test_manual_switch_quits_and_relaunches(tmp_path: Path):
    follower, data = _connect_two(tmp_path)
    follower.app.running = True
    follower.app.calls.clear()

    result = follower.follow("a@x.com", "org-a", allow_restart=True)

    assert result["action"] == "switched"
    assert follower.app.calls == ["quit", "launch"]
    assert (data / "config.json").read_text() == "a"


def test_app_that_will_not_quit_is_left_alone(tmp_path: Path):
    follower, data = _connect_two(tmp_path)
    follower.app.running = True
    follower.app.quits = False

    result = follower.follow("a@x.com", "org-a", allow_restart=True)

    assert result["action"] == "skipped"
    assert (data / "config.json").read_text() == "b"


def test_unconnected_account_changes_nothing(tmp_path: Path):
    follower, data = _connect_two(tmp_path)

    result = follower.follow("c@x.com", "org-c", allow_restart=True)

    assert result["action"] == "none"
    assert (data / "config.json").read_text() == "b"


def test_off_stops_following(tmp_path: Path):
    follower, data = _connect_two(tmp_path)
    follower.set_enabled(False)

    assert follower.follow("a@x.com", "org-a", allow_restart=False)["action"] == "none"
    assert (data / "config.json").read_text() == "b"
    assert not follower.is_active()


def test_failed_swap_rolls_back_both_accounts(tmp_path: Path):
    follower, data = _connect_two(tmp_path)
    parked_a = follower.parked_dir(account_key("a@x.com", "org-a"))
    # A shared entry name inside a parked login collides on the way in.
    (parked_a / "vm_bundles").mkdir()

    result = follower.follow("a@x.com", "org-a", allow_restart=False)

    assert result["action"] == "error"
    assert (data / "config.json").read_text() == "b"
    assert (data / "Local Storage" / "leveldb").read_text() == "b"
    assert (parked_a / "config.json").read_text() == "a"
    assert (data / "vm_bundles" / "disk.img").read_text() == "shared"
    assert follower.load()["current"] == account_key("b@x.com", "org-b")


def test_follow_active_is_silent_without_desktop_state(tmp_path: Path):
    switcher = SimpleNamespace(
        backup_dir=tmp_path / "backup",
        _get_current_account=lambda: ("a@x.com", "org-a"),
    )

    assert follow_active(switcher, allow_restart=True) is None
    assert not (tmp_path / "backup" / "desktop").exists()
