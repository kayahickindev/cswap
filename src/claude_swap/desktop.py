"""Make the Claude desktop app follow cswap's active account (macOS).

Claude Desktop knows one sign-in at a time, kept in
``~/Library/Application Support/Claude``. cswap gives it one per account: the
sign-in files of every account the app is NOT on are parked under
``<backup_dir>/desktop/logins/<id>/``, and a switch moves the outgoing set out
and the incoming set in. Heavy or account-neutral entries (the Cowork VM,
caches, the MCP server config, the Code-tab session index, which the app
already files per account) never move, so every account shares them.

Every move is a rename inside one volume: nothing is copied, a swap takes
milliseconds, and an interrupted swap is rolled back move by move. The app
must be closed while it happens. A manual switch quits and relaunches it; the
auto-switch loop never restarts a running app (it cannot tell whether a chat
is mid-reply) and applies the swap the next time it finds the app closed.

An account follows only after it is connected once (``cswap desktop
connect``), which records which account the app is signed in as right now.
Identity is the stable (email, organizationUuid) composite, as in
``mappings``, never the slot number.

This relies on undocumented app storage, so it fails soft: every problem is
logged and returned in the result, never raised, and the Claude Code switch it
rides on is never affected. A parked login is never deleted; anything in the
way is moved aside, not removed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from claude_swap.models import get_timestamp
from claude_swap.settings import atomic_write_json

SCHEMA_VERSION = 1
BUNDLE_ID = "com.anthropic.claudefordesktop"
QUIT_TIMEOUT_S = 30.0

_logger = logging.getLogger("claude-swap")

# Entries of the app's data directory that stay in place for every account.
# Everything else moves with the account, so a sign-in file a future app
# version adds is parked by default instead of leaking across accounts.
SHARED_ENTRIES = frozenset({
    ".DS_Store",
    "Cache",
    "Code Cache",
    "Crashpad",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GPUCache",
    "Shared Dictionary",
    "ca-bundle.pem",
    "claude-code",
    "claude-code-sessions",
    "claude-code-vm",
    "claude_desktop_config.json",
    "claude_desktop_config.json.bak",
    "extensions-blocklist.json",
    "git-worktrees.json",
    "sentry",
    "vm_bundles",
    "window-state.json",
})


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Claude"


def account_key(email: str, org_uuid: str | None) -> str:
    return f"{email.strip().lower()}|{org_uuid or ''}"


def _login_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class DesktopApp:
    """Process control for the desktop app. Swapped for a fake in tests."""

    def is_running(self) -> bool:
        out = subprocess.run(
            ["osascript", "-e", f'application id "{BUNDLE_ID}" is running'],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() == "true"

    def quit(self, timeout: float = QUIT_TIMEOUT_S) -> bool:
        """Ask the app to quit and wait for it; True once it is gone."""
        subprocess.run(
            ["osascript", "-e", f'tell application id "{BUNDLE_ID}" to quit'],
            capture_output=True, text=True, timeout=timeout,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running():
                # The main process exits a moment before its helpers have
                # flushed their last writes into the data directory.
                time.sleep(1.0)
                return True
            time.sleep(0.5)
        return False

    def launch(self) -> None:
        subprocess.run(["open", "-b", BUNDLE_ID], capture_output=True, timeout=30)


class DesktopFollower:
    """Reads and writes ``<backup_dir>/desktop/`` and swaps the app's sign-in."""

    def __init__(
        self,
        backup_dir: Path,
        data_dir: Path | None = None,
        app: DesktopApp | None = None,
    ):
        self.root = Path(backup_dir) / "desktop"
        self.state_path = self.root / "state.json"
        self.logins_dir = self.root / "logins"
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.app = app or DesktopApp()

    # -- state -------------------------------------------------------------

    def load(self) -> dict:
        """Return the state (defaults on missing/corrupt)."""
        state = {"enabled": False, "current": None, "pending": None, "accounts": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
            return state
        if isinstance(data, dict):
            state["enabled"] = bool(data.get("enabled", False))
            state["current"] = data.get("current") or None
            state["pending"] = data.get("pending") or None
            accounts = data.get("accounts")
            state["accounts"] = accounts if isinstance(accounts, dict) else {}
        return state

    def _save(self, state: dict) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_json(self.state_path, {"schemaVersion": SCHEMA_VERSION, **state})

    def is_active(self) -> bool:
        """Cheap gate for the auto loop: anything to follow at all?"""
        if not self.state_path.exists():
            return False
        state = self.load()
        return state["enabled"] and bool(state["accounts"])

    def parked_dir(self, key: str) -> Path:
        return self.logins_dir / _login_id(key)

    def _account_entries(self) -> list[Path]:
        if not self.data_dir.is_dir():
            return []
        return sorted(p for p in self.data_dir.iterdir() if p.name not in SHARED_ENTRIES)

    def has_login(self) -> bool:
        """Whether the app's data directory holds a sign-in right now."""
        return (self.data_dir / "config.json").exists()

    # -- commands ----------------------------------------------------------

    def connect(self, email: str, org_uuid: str | None) -> dict:
        """Record that the app is signed in as this account right now."""
        if not self.has_login():
            return _result("skipped", "the desktop app has no sign-in to connect; open it and sign in first")
        key = account_key(email, org_uuid)
        state = self.load()
        previous = state["current"]
        if previous and previous != key and not self.parked_dir(previous).is_dir():
            # The app was signed in as `previous` and someone signed it in as
            # this account by hand: that login is gone, so is its record.
            state["accounts"].pop(previous, None)
        if self.parked_dir(key).is_dir():
            # A stale parked copy of the account now live in the app.
            self._move_aside(self.parked_dir(key))
        state["accounts"][key] = {
            "email": email,
            "organizationUuid": org_uuid or "",
            "connectedAt": get_timestamp(),
        }
        state["current"] = key
        state["pending"] = None
        state["enabled"] = True
        self._save(state)
        _logger.info("desktop: connected %s", email)
        return _result("connected", f"the desktop app follows {email} from now on")

    def new_login(self) -> dict:
        """Park the current sign-in and open the app signed out."""
        state = self.load()
        current = state["current"]
        if self.has_login() and not current:
            return _result("skipped", "connect the account the app is signed in as first (cswap desktop connect)")
        if self.app.is_running() and not self.app.quit():
            return _result("skipped", "the desktop app did not quit")
        if current and self.has_login():
            self._park(current)
        state["current"] = None
        state["pending"] = None
        self._save(state)
        self.app.launch()
        return _result("signed-out", "sign in to the desktop app, then run: cswap desktop connect <account>")

    def set_enabled(self, enabled: bool) -> dict:
        state = self.load()
        state["enabled"] = enabled
        if not enabled:
            state["pending"] = None
        self._save(state)
        return _result("on" if enabled else "off", "")

    def follow(self, email: str, org_uuid: str | None, *, allow_restart: bool) -> dict:
        """Put the app on this account if it is connected. Never raises."""
        try:
            return self._follow(account_key(email, org_uuid), email, allow_restart)
        except Exception as e:  # undocumented storage: never fail the caller
            _logger.warning("desktop: follow failed: %s: %s", type(e).__name__, e)
            return _result("error", f"{type(e).__name__}: {e}")

    def _follow(self, key: str, email: str, allow_restart: bool) -> dict:
        state = self.load()
        if not state["enabled"] or not state["accounts"]:
            return _result("none", "off")
        if key not in state["accounts"]:
            return _result("none", f"{email} is not connected to the desktop app")
        if state["current"] == key:
            if state["pending"]:
                state["pending"] = None
                self._save(state)
            return _result("none", "already on this account")
        if not state["current"]:
            return _result("none", "the desktop app's sign-in is not connected yet")
        if not self.parked_dir(key).is_dir():
            return _result("skipped", f"no parked sign-in for {email}; reconnect it")

        relaunch = False
        if self.app.is_running():
            if not allow_restart:
                if state["pending"] != key:
                    state["pending"] = key
                    self._save(state)
                    _logger.info("desktop: %s waits until the app is closed", email)
                return _result("deferred", "the desktop app is open; it switches when you close it")
            if not self.app.quit():
                return _result("skipped", "the desktop app did not quit")
            relaunch = True

        self._swap(state["current"], key)
        state["current"] = key
        state["pending"] = None
        self._save(state)
        _logger.info("desktop: switched to %s", email)
        if relaunch:
            self.app.launch()
        return _result("switched", f"the desktop app is on {email}")

    # -- moves -------------------------------------------------------------

    def _swap(self, outgoing: str, incoming: str) -> None:
        source = self.parked_dir(incoming)
        if os.stat(source).st_dev != os.stat(self.data_dir).st_dev:
            raise OSError("parked sign-ins and the app data are on different volumes")
        parked = self._park(outgoing)
        try:
            self._unpark(source)
        except Exception:
            # Put the outgoing account back exactly as it was.
            self._unpark(parked)
            raise

    def _park(self, key: str) -> Path:
        dest = self.parked_dir(key)
        if dest.exists():
            self._move_aside(dest)
        dest.mkdir(mode=0o700, parents=True)
        moved: list[tuple[Path, Path]] = []
        try:
            for src in self._account_entries():
                target = dest / src.name
                os.rename(src, target)
                moved.append((src, target))
        except Exception:
            for src, target in reversed(moved):
                os.rename(target, src)
            dest.rmdir()
            raise
        return dest

    def _unpark(self, source: Path) -> None:
        moved: list[tuple[Path, Path]] = []
        try:
            for src in sorted(source.iterdir()):
                target = self.data_dir / src.name
                if target.exists() or target.is_symlink():
                    raise FileExistsError(f"{target} is in the way")
                os.rename(src, target)
                moved.append((src, target))
        except Exception:
            for src, target in reversed(moved):
                os.rename(target, src)
            raise
        source.rmdir()

    def _move_aside(self, path: Path) -> None:
        aside = path.with_name(f"{path.name}.aside-{time.strftime('%Y%m%d-%H%M%S')}")
        os.rename(path, aside)
        _logger.warning("desktop: moved %s aside to %s", path.name, aside.name)


def _result(action: str, detail: str) -> dict:
    return {"action": action, "detail": detail}


def follow_active(switcher, *, allow_restart: bool) -> dict | None:
    """Follow the switcher's live account; None when desktop follow is off.

    Shared by the manual switch path (``allow_restart=True``) and every
    auto-switch loop tick (``False``). Never raises.
    """
    try:
        follower = DesktopFollower(switcher.backup_dir)
        if not follower.is_active():
            return None
        identity = switcher._get_current_account()
        if identity is None:
            return None
        email, org_uuid = identity
        return follower.follow(email, org_uuid, allow_restart=allow_restart)
    except Exception as e:
        _logger.warning("desktop: follow_active failed: %s: %s", type(e).__name__, e)
        return None
