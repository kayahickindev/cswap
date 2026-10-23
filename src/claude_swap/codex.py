"""Rotate the Codex CLI between several ChatGPT logins.

Codex reads one login, ``$CODEX_HOME/auth.json`` (``~/.codex`` by default),
when a session starts. cswap keeps every login in its own folder and makes
the live ``auth.json`` a symlink to the active one. Three properties of Codex
(codex-rs ``login/src/auth``, checked against 0.163.0) make that safe:

- Codex saves refreshed tokens by opening ``auth.json`` in place, so the write
  goes through the symlink into the account's own file. Each login has exactly
  one copy of its single-use refresh token, and nothing goes stale.
- Before a refresh, a running session re-reads ``auth.json`` and stops if it
  now belongs to another account (``reload_if_account_id_matches``), and it
  never saves over a different account (``persist_tokens``). A session still
  running on the old login can neither burn nor overwrite the new one.
- A session keeps the login it started with, so a switch reaches new Codex
  sessions; running ones change account when restarted (``codex resume``).

Usage comes from Codex itself: ``codex app-server`` run against an account's
folder answers ``account/rateLimits/read`` for that login without making it
the live one.

One Codex home rotates (``cswap codex home``, ``~/.codex`` by default). Any
other home left linked to a slot is pinned to that login: it keeps it, and the
rotation never takes that login. With the Daybreak home rotating between the
workspace seats, plain ``codex`` stays on Pro.

Run ``codex login`` only through ``cswap codex add``: a plain ``codex login``
writes through the symlink and replaces the active account's login.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.locking import FileLock

SCHEMA_VERSION = 1
AUTH_FILENAME = "auth.json"

# One app-server start plus two requests; a cold start takes a second or two.
USAGE_TIMEOUT_S = 20.0

# Where the Codex installers put the binary; launchd starts cswap with a bare
# PATH.
_CODEX_PATHS = (
    "~/.local/bin/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
)

# Window lengths Codex reports, in minutes.
_FIVE_HOUR_MAX_MINS = 600


class CodexError(ClaudeSwitchError):
    pass


def find_codex() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    for candidate in _CODEX_PATHS:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def live_codex_home() -> Path:
    home = os.environ.get("CODEX_HOME")
    return Path(home).expanduser() if home else Path.home() / ".codex"


def _iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodexIdentity:
    """Who a stored login is, read from its id_token claims (not verified:
    it only labels a file Codex itself already trusts)."""

    email: str
    account_id: str
    user_id: str
    plan: str

    @property
    def key(self) -> tuple[str, str]:
        # One person can hold a Pro login and seats in several workspaces.
        return (self.user_id or self.email.lower(), self.account_id)


def read_identity(auth_file: Path) -> CodexIdentity | None:
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    claims = _jwt_claims(tokens.get("id_token"))
    auth = claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    account_id = tokens.get("account_id") or auth.get("chatgpt_account_id") or ""
    email = claims.get("email") or ""
    if not (email or account_id):
        return None
    return CodexIdentity(
        email=str(email),
        account_id=str(account_id),
        user_id=str(auth.get("chatgpt_user_id") or auth.get("user_id") or ""),
        plan=str(auth.get("chatgpt_plan_type") or ""),
    )


def _jwt_claims(token: object) -> dict:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodexWindow:
    label: str  # "5h" or "7d", or "<limit> 5h" for a named per-model limit
    pct: float
    resets_at: float | None


@dataclass(frozen=True)
class CodexUsage:
    windows: tuple[CodexWindow, ...]
    # The backend's own verdict on included usage; None when it gave none.
    usage_allowed: bool | None
    limit_reached: bool
    fetched_at: float
    error: str | None = None

    @property
    def binding_pct(self) -> float | None:
        if self.error is not None:
            return None
        if self.usage_allowed is False or self.limit_reached:
            return 100.0
        account_wide = [w.pct for w in self.windows if " " not in w.label]
        return max(account_wide or [w.pct for w in self.windows] or [0.0])

    @property
    def weekly_reset(self) -> float | None:
        resets = [w.resets_at for w in self.windows if w.label == "7d" and w.resets_at]
        return min(resets) if resets else None

    @property
    def five_hour_pct(self) -> float | None:
        """The account-wide 5-hour window's utilization, the renewable axis
        the Daybreak rotation drains. None when the seat reports no 5h window
        (a Pro login reports only a weekly window)."""
        if self.error is not None:
            return None
        for window in self.windows:
            if window.label == "5h":
                return window.pct
        return None

    @property
    def five_hour_reset(self) -> float | None:
        for window in self.windows:
            if window.label == "5h" and window.resets_at:
                return window.resets_at
        return None

    @property
    def weekly_pct(self) -> float | None:
        pcts = [w.pct for w in self.windows if w.label == "7d"]
        return max(pcts) if pcts else None

    @property
    def weekly_blocked(self) -> bool:
        """No real room left this week, whatever the 5-hour window says: the
        backend refused included usage, or the weekly window is itself full.
        Such a seat is never a rotation target even with a fresh 5h window."""
        if self.usage_allowed is False or self.limit_reached:
            return True
        weekly = self.weekly_pct
        return weekly is not None and weekly >= 100.0

    def rotation_pct(self, axis: str) -> float | None:
        """The utilization the rotation decides on. ``five_hour`` drains each
        seat's renewable 5-hour window and treats the weekly as a hard wall;
        ``binding`` is the old max-of-windows behavior. A weekly-blocked seat
        always reads 100 so it is never chosen or held."""
        if axis != "five_hour":
            return self.binding_pct
        if self.error is not None:
            return None
        if self.weekly_blocked:
            return 100.0
        five = self.five_hour_pct
        return five if five is not None else self.binding_pct

    def to_json(self) -> dict:
        return {
            "windows": [
                {
                    "label": w.label,
                    "pct": w.pct,
                    "resetsAt": _iso(w.resets_at) if w.resets_at else None,
                }
                for w in self.windows
            ],
            "usageAllowed": self.usage_allowed,
            "limitReached": self.limit_reached,
            "bindingPct": self.binding_pct,
            "fetchedAt": _iso(self.fetched_at),
            "error": self.error,
        }

    @classmethod
    def from_json(cls, data: dict) -> "CodexUsage | None":
        try:
            windows = tuple(
                CodexWindow(
                    label=w["label"],
                    pct=float(w["pct"]),
                    resets_at=_epoch(w.get("resetsAt")),
                )
                for w in data.get("windows") or []
            )
            fetched = _epoch(data.get("fetchedAt"))
            return cls(
                windows=windows,
                usage_allowed=data.get("usageAllowed"),
                limit_reached=bool(data.get("limitReached")),
                fetched_at=fetched or 0.0,
                error=data.get("error"),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_rate_limits(result: dict, now: float) -> CodexUsage:
    """Map an ``account/rateLimits/read`` result to windows.

    ``rateLimitsByLimitId`` is sometimes empty, so the top-level snapshot is
    the fallback (the same rule nl-usage uses).
    """
    snapshots = result.get("rateLimitsByLimitId") or {
        "codex": result.get("rateLimits") or {}
    }
    windows: list[CodexWindow] = []
    reached = False
    for snapshot in snapshots.values():
        if not isinstance(snapshot, dict):
            continue
        if snapshot.get("rateLimitReachedType"):
            reached = True
        name = snapshot.get("limitName")
        prefix = f"{name.split('-')[-1]} " if name else ""
        for window in (snapshot.get("primary"), snapshot.get("secondary")):
            if not isinstance(window, dict):
                continue
            minutes = window.get("windowDurationMins") or 0
            label = "5h" if minutes <= _FIVE_HOUR_MAX_MINS else "7d"
            resets = window.get("resetsAt")
            windows.append(
                CodexWindow(
                    label=prefix + label,
                    pct=float(window.get("usedPercent") or 0),
                    resets_at=float(resets) if isinstance(resets, (int, float)) else None,
                )
            )
    return CodexUsage(
        windows=tuple(windows),
        usage_allowed=result.get("ordinaryUsageAllowed"),
        limit_reached=reached,
        fetched_at=now,
    )


def read_usage(
    codex: str, codex_home: Path, *, timeout: float = USAGE_TIMEOUT_S
) -> CodexUsage:
    """Ask ``codex app-server`` running on ``codex_home`` for its limits."""
    now = time.time()
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"clientInfo":{"name":"cswap","title":"cswap","version":"1"}}}\n'
        '{"jsonrpc":"2.0","method":"initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read",'
        '"params":{"excludeResetCreditDetails":true}}\n'
    )
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    try:
        proc = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
    except OSError as e:
        return CodexUsage((), None, False, now, error=f"codex failed to start: {e}")
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(requests)
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") != 2:
                continue
            if "error" in message:
                detail = (message.get("error") or {}).get("message") or "error"
                return CodexUsage((), None, False, now, error=str(detail))
            return parse_rate_limits(message.get("result") or {}, now)
        return CodexUsage((), None, False, now, error="no answer from codex app-server")
    except OSError as e:
        return CodexUsage((), None, False, now, error=str(e))
    finally:
        watchdog.cancel()
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class CodexAccount:
    number: str
    email: str
    account_id: str
    user_id: str
    plan: str
    added: str
    usage: CodexUsage | None = None
    active: bool = False
    # Other Codex homes that keep this login; the rotation leaves it alone.
    pinned_to: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        return (self.user_id or self.email.lower(), self.account_id)

    def to_json(self) -> dict:
        return {
            "number": int(self.number),
            "email": self.email,
            "plan": self.plan,
            "accountId": self.account_id,
            "active": self.active,
            "pinnedTo": list(self.pinned_to),
            "usage": self.usage.to_json() if self.usage else None,
        }


UsageReader = Callable[[Path], CodexUsage]


class CodexStore:
    """Codex logins under ``<backup_root>/codex``, one folder per account."""

    def __init__(
        self,
        backup_root: Path,
        *,
        live_home: Path | None = None,
        codex: str | None = None,
        usage_reader: UsageReader | None = None,
        login_runner: Callable[[Path], int] | None = None,
    ):
        self.root = Path(backup_root) / "codex"
        self.slots = self.root / "slots"
        self.index_path = self.root / "accounts.json"
        self.usage_path = self.root / "usage.json"
        self._explicit_home = Path(live_home) if live_home is not None else None
        self._codex = codex
        self._usage_reader = usage_reader
        self._login_runner = login_runner
        self._lock = FileLock(self.root / ".lock")

    # -- paths -------------------------------------------------------------

    @property
    def live_home(self) -> Path:
        """The Codex home that rotates: as configured, else CODEX_HOME or ~/.codex."""
        if self._explicit_home is not None:
            return self._explicit_home
        configured = self._read_index().get("liveHome")
        return Path(configured).expanduser() if configured else live_codex_home()

    @property
    def live_auth(self) -> Path:
        return self.live_home / AUTH_FILENAME

    def slot_dir(self, number: str) -> Path:
        return self.slots / str(number)

    def slot_auth(self, number: str) -> Path:
        return self.slot_dir(number) / AUTH_FILENAME

    @property
    def codex(self) -> str:
        if self._codex is None:
            self._codex = find_codex()
        if not self._codex:
            raise CodexError("codex is not installed (looked on PATH and in ~/.local/bin)")
        return self._codex

    # -- index -------------------------------------------------------------

    def _read_index(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schemaVersion": SCHEMA_VERSION, "accounts": {}}
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            return {"schemaVersion": SCHEMA_VERSION, "accounts": {}}
        return data

    def _write_index(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        _atomic_write(self.index_path, json.dumps(data, indent=2) + "\n")

    def accounts(self) -> list[CodexAccount]:
        data = self._read_index()
        active = self.active_number()
        usage = self.cached_usage()
        pins = self.pins()
        accounts = []
        for number, record in sorted(data["accounts"].items(), key=lambda kv: int(kv[0])):
            accounts.append(
                CodexAccount(
                    number=number,
                    email=record.get("email", ""),
                    account_id=record.get("accountId", ""),
                    user_id=record.get("userId", ""),
                    plan=record.get("plan", ""),
                    added=record.get("added", ""),
                    usage=usage.get(number),
                    active=number == active,
                    pinned_to=tuple(pins.get(number, ())),
                )
            )
        return accounts

    def resolve(self, target: str) -> CodexAccount:
        accounts = self.accounts()
        for account in accounts:
            if account.number == target.strip():
                return account
        matches = [a for a in accounts if a.email.lower() == target.strip().lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            numbers = ", ".join(a.number for a in matches)
            raise CodexError(f"{target} has several Codex logins ({numbers}); pick one by number")
        raise CodexError(f"no Codex account {target!r} (see: cswap codex list)")

    def active_number(self) -> str | None:
        """The slot the rotating home's auth.json points at, if it is ours."""
        return self._slot_of_link(self.live_auth)

    def pins(self) -> dict[str, list[str]]:
        """Slot number -> the other homes still linked to it."""
        out: dict[str, list[str]] = {}
        live = str(self.live_home)
        for home in self._read_index().get("pinned", []):
            if home == live:
                continue
            number = self._slot_of_link(Path(home) / AUTH_FILENAME)
            if number is not None:
                out.setdefault(number, []).append(home)
        return out

    def set_live_home(self, home: Path) -> CodexAccount | None:
        """Make ``home`` the rotating Codex home.

        The home that rotated until now keeps the login it points at, pinned.
        A plain login already in ``home`` is stored as an account.
        """
        home = Path(home).expanduser().absolute()
        with self._lock:
            data = self._read_index()
            old = self.live_home
            pinned = set(data.get("pinned", []))
            if old != home and self._slot_of_link(old / AUTH_FILENAME) is not None:
                pinned.add(str(old))
            pinned.discard(str(home))
            data["pinned"] = sorted(pinned)
            if str(old) != str(home):
                # The switch history belongs to the home that rotated until now.
                data.pop("lastSwitchAt", None)
                data["switchLog"] = []
            data["liveHome"] = str(home)
            self._write_index(data)
            if self._explicit_home is not None:
                self._explicit_home = home
        live = home / AUTH_FILENAME
        if live.exists() and not live.is_symlink():
            return self.adopt_live()
        number = self.active_number()
        return self.resolve(number) if number else None

    def _slot_of_link(self, auth: Path) -> str | None:
        try:
            if not auth.is_symlink():
                return None
            target = Path(os.readlink(auth))
        except OSError:
            return None
        if not target.is_absolute():
            target = auth.parent / target
        try:
            relative = target.resolve().relative_to(self.slots.resolve())
        except (OSError, ValueError):
            return None
        parts = relative.parts
        if len(parts) == 2 and parts[1] == AUTH_FILENAME:
            return parts[0]
        return None

    def _next_number(self, data: dict) -> str:
        used = {int(n) for n in data["accounts"]}
        number = 1
        while number in used:
            number += 1
        return str(number)

    def _record(self, identity: CodexIdentity) -> dict:
        return {
            "email": identity.email,
            "accountId": identity.account_id,
            "userId": identity.user_id,
            "plan": identity.plan,
            "added": _iso(time.time()),
        }

    def _find_by_key(self, data: dict, identity: CodexIdentity) -> str | None:
        for number, record in data["accounts"].items():
            key = (record.get("userId") or record.get("email", "").lower(), record.get("accountId", ""))
            if key == identity.key:
                return number
        return None

    # -- adding ------------------------------------------------------------

    def adopt_live(self) -> CodexAccount:
        """Take the login Codex is using now into a slot and link it back.

        The file is hard-linked into the slot, then the live path is swapped
        for a symlink in one rename, so Codex never sees auth.json missing and
        no second copy of the refresh token ever exists.
        """
        with self._lock:
            active = self.active_number()
            if active is not None:
                return self.resolve(active)
            if self.live_auth.is_symlink():
                raise CodexError(
                    f"{self.live_auth} is a symlink cswap does not manage; "
                    "remove it or point it at a real login first"
                )
            if not self.live_auth.exists():
                raise CodexError(f"Codex is not logged in ({self.live_auth} is missing)")
            identity = read_identity(self.live_auth)
            if identity is None:
                raise CodexError(f"{self.live_auth} is not a ChatGPT login cswap can read")
            data = self._read_index()
            number = self._find_by_key(data, identity)
            if number is not None:
                # Same login stored before: the live file is the newer copy.
                self._retire_slot_file(number)
            else:
                number = self._next_number(data)
            self._prepare_slot(number)
            target = self.slot_auth(number)
            try:
                os.link(self.live_auth, target)
            except OSError:
                shutil.copy2(self.live_auth, target)
            os.chmod(target, 0o600)
            self._point_live_at(number)
            data["accounts"][number] = self._record(identity)
            self._write_index(data)
        return self.resolve(number)

    def add_login(self) -> CodexAccount:
        """Run ``codex login`` into a fresh folder and store the result."""
        if self.live_auth.exists() and self.active_number() is None:
            # Keep the current login before anything else touches Codex.
            self.adopt_live()
        self.slots.mkdir(parents=True, exist_ok=True)
        staging = self.slots / f".login-{os.getpid()}-{int(time.time())}"
        staging.mkdir(mode=0o700)
        try:
            code = (self._login_runner or self._run_login)(staging)
            auth = staging / AUTH_FILENAME
            if code != 0 or not auth.exists():
                raise CodexError("codex login did not finish; nothing was added")
            identity = read_identity(auth)
            if identity is None:
                raise CodexError("codex login wrote a login cswap cannot read")
            with self._lock:
                data = self._read_index()
                # A re-login of a stored account replaces its file in one
                # rename, so a live link to it never dangles.
                number = self._find_by_key(data, identity) or self._next_number(data)
                self._prepare_slot(number)
                os.replace(auth, self.slot_auth(number))
                os.chmod(self.slot_auth(number), 0o600)
                data["accounts"][number] = self._record(identity)
                self._write_index(data)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return self.resolve(number)

    def _run_login(self, codex_home: Path) -> int:
        env = dict(os.environ, CODEX_HOME=str(codex_home))
        return subprocess.run([self.codex, "login"], env=env, check=False).returncode

    def _prepare_slot(self, number: str) -> None:
        self.slots.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.slot_dir(number).mkdir(mode=0o700, exist_ok=True)

    def _retire_slot_file(self, number: str) -> None:
        old = self.slot_auth(number)
        if old.exists():
            old.unlink()

    # -- switching ---------------------------------------------------------

    def switch(self, target: str) -> tuple[CodexAccount | None, CodexAccount]:
        """Point the live auth.json at ``target``'s login. Returns (from, to)."""
        if self.live_auth.exists() and not self.live_auth.is_symlink():
            # Store an unmanaged login (a plain `codex login`) before the
            # live path stops pointing at it.
            self.adopt_live()
        account = self.resolve(target)
        if account.pinned_to:
            raise CodexError(
                f"Codex account {account.number} is pinned to {', '.join(account.pinned_to)}; "
                "use that home for it"
            )
        with self._lock:
            if not self.slot_auth(account.number).exists():
                raise CodexError(
                    f"Codex account {account.number} has no stored login; "
                    "add it again with: cswap codex add"
                )
            previous = self.active_number()
            if previous == account.number:
                return account, account
            self._point_live_at(account.number)
            data = self._read_index()
            now = time.time()
            data["lastSwitchAt"] = now
            data["lastSwitchFrom"] = previous
            data["lastSwitchTo"] = account.number
            log = data.get("switchLog") if isinstance(data.get("switchLog"), list) else []
            data["switchLog"] = (log + [{"at": now, "from": previous, "to": account.number}])[-100:]
            self._write_index(data)
        before = None
        if previous is not None:
            try:
                before = self.resolve(previous)
            except CodexError:
                before = None
        return before, self.resolve(account.number)

    def _point_live_at(self, number: str) -> None:
        self.live_home.mkdir(parents=True, exist_ok=True)
        tmp = self.live_home / f".auth.json.cswap-{os.getpid()}"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(self.slot_auth(number), tmp)
        os.replace(tmp, self.live_auth)

    def remove(self, target: str) -> CodexAccount:
        account = self.resolve(target)
        with self._lock:
            if account.number == self.active_number():
                raise CodexError("that is the active Codex login; switch to another one first")
            if account.pinned_to:
                raise CodexError(f"that login is pinned to {', '.join(account.pinned_to)}")
            data = self._read_index()
            data["accounts"].pop(account.number, None)
            self._write_index(data)
            shutil.rmtree(self.slot_dir(account.number), ignore_errors=True)
            usage = self._read_usage_cache()
            usage.pop(account.number, None)
            self._write_usage_cache(usage)
        return account

    def check_slots(self) -> list[str]:
        """Relabel any slot whose login changed under it (a plain
        ``codex login`` writes through the symlink); return what changed."""
        notes = []
        with self._lock:
            data = self._read_index()
            for number, record in data["accounts"].items():
                identity = read_identity(self.slot_auth(number))
                if identity is None:
                    continue
                stored = (record.get("userId") or record.get("email", "").lower(), record.get("accountId", ""))
                if identity.key != stored:
                    notes.append(
                        f"Codex account {number} now holds {identity.email} "
                        f"(was {record.get('email')}); a plain `codex login` replaced it"
                    )
                    data["accounts"][number] = {**self._record(identity), "added": record.get("added")}
            if notes:
                self._write_index(data)
        return notes

    # -- usage -------------------------------------------------------------

    def _read_usage_cache(self) -> dict:
        try:
            data = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_usage_cache(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.usage_path, json.dumps(data, indent=2) + "\n")

    def cached_usage(self) -> dict[str, CodexUsage]:
        out = {}
        for number, value in self._read_usage_cache().items():
            usage = CodexUsage.from_json(value) if isinstance(value, dict) else None
            if usage is not None:
                out[number] = usage
        return out

    def refresh_usage(self, numbers: Sequence[str] | None = None) -> dict[str, CodexUsage]:
        """Read usage for ``numbers`` (all accounts by default), in parallel,
        and keep the last good reading of any account whose read failed."""
        accounts = {a.number: a for a in self.accounts()}
        wanted = [n for n in (numbers or list(accounts)) if n in accounts]
        reader = self._usage_reader or (lambda home: read_usage(self.codex, home))
        results: dict[str, CodexUsage] = {}
        if wanted:
            with ThreadPoolExecutor(max_workers=min(6, len(wanted))) as pool:
                for number, usage in zip(
                    wanted, pool.map(lambda n: reader(self.slot_dir(n)), wanted)
                ):
                    results[number] = usage
        with self._lock:
            cache = self._read_usage_cache()
            for number, usage in results.items():
                previous = cache.get(number)
                if usage.error is not None and isinstance(previous, dict) and not previous.get("error"):
                    continue
                cache[number] = usage.to_json()
            for number in list(cache):
                if number not in accounts:
                    cache.pop(number)
            self._write_usage_cache(cache)
        return results


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Auto-switch
# ---------------------------------------------------------------------------


@dataclass
class CodexAutoSettings:
    threshold: float = 90.0
    interval_seconds: float = 60.0
    # Accounts that are not live move slowly; read them this often.
    candidate_interval_seconds: float = 300.0
    cooldown_seconds: float = 300.0
    # Which window the rotation drains: "five_hour" (default) keeps each seat
    # on its renewable 5-hour window and treats the weekly as a hard wall, so
    # two Daybreak seats spend both 5-hour windows fully; "binding" is the old
    # max-of-windows behavior.
    decide_on: str = "five_hour"


@dataclass
class CodexAutoEngine:
    """Keep new Codex sessions on a login with room.

    The live login moves when the decision window (the 5-hour window by
    default, ``settings.decide_on``) reaches the threshold or the backend says
    its included usage is spent, to the account with the most room on that
    window, ties to the soonest reset. A weekly-blocked seat is never a target
    even with a fresh 5-hour window. The cooldown never holds a seat already
    out.
    """

    store: CodexStore
    settings: CodexAutoSettings
    emit: Callable[[dict], None]
    dry_run: bool = False
    clock: Callable[[], float] = time.time
    # Restarts Codex panes still on an older login (codex_panes.CodexPaneSync).
    pane_sync: object | None = None
    _last_full_read: float = field(default=0.0, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def tick(self) -> str:
        outcome = self._decide()
        if self.pane_sync is not None and not self.dry_run:
            try:
                for result in self.pane_sync.sync():
                    self._event("restart", **result)
            except Exception as e:  # a pane restart must never stop the switcher
                self._event("error", message=f"pane sync: {type(e).__name__}: {e}", transient=True)
        return outcome

    def _decide(self) -> str:
        now = self.clock()
        for note in self.store.check_slots():
            self._event("warning", message=note)
        accounts = self.store.accounts()
        if not accounts:
            self._event("no-switch", reason="no-accounts", detail="add one with: cswap codex add")
            return "no-action"
        active = next((a for a in accounts if a.active), None)
        if active is None:
            self._event(
                "no-switch",
                reason="unmanaged-login",
                detail="the live Codex login is not one of cswap's (run: cswap codex add --adopt)",
            )
            return "no-action"

        full = now - self._last_full_read >= self.settings.candidate_interval_seconds
        numbers = [a.number for a in accounts] if full else [active.number]
        self.store.refresh_usage(numbers)
        if full:
            self._last_full_read = now
        accounts = self.store.accounts()
        active = next(a for a in accounts if a.active)
        axis = self.settings.decide_on
        pct = lambda a: a.usage.rotation_pct(axis) if a.usage else None
        rank_reset = (
            (lambda a: a.usage.five_hour_reset) if axis == "five_hour"
            else (lambda a: a.usage.weekly_reset)
        )
        self._event(
            "poll",
            active=int(active.number),
            threshold=self.settings.threshold,
            axis=axis,
            usedPct={a.number: pct(a) for a in accounts},
        )

        used = pct(active)
        if used is None:
            self._event("no-switch", reason="active-usage-unknown",
                        detail=(active.usage.error if active.usage else "not read yet"))
            return "no-action"
        if used < self.settings.threshold:
            self._event("no-switch", reason="below-threshold",
                        detail=f"{used:.0f}% < {self.settings.threshold:.0f}%")
            return "no-action"
        exhausted = used >= 100.0
        if not exhausted and self._in_cooldown(now):
            self._event("no-switch", reason="cooldown")
            return "no-action"

        if not full:
            # Decide on fresh numbers for everyone, not five-minute-old ones.
            self.store.refresh_usage([a.number for a in accounts if not a.active])
            self._last_full_read = now
            accounts = self.store.accounts()
        usable = [
            a for a in accounts
            if not a.active and not a.pinned_to and a.usage is not None
            and pct(a) is not None
        ]
        candidates = [a for a in usable if pct(a) < self.settings.threshold]
        if not candidates and exhausted:
            # The active seat is hard out. Any peer with room left on the
            # decision window (and not walled by the weekly) beats sitting on a
            # dead seat, even though it is over the proactive line.
            candidates = [
                a for a in usable
                if pct(a) < 100.0 and not a.usage.weekly_blocked
            ]
        if not candidates:
            self._event("no-switch", reason="no-candidates",
                        detail="every other Codex login is at its limit or unread")
            return "blocked"
        target = min(
            candidates,
            key=lambda a: (pct(a), rank_reset(a) or float("inf"), int(a.number)),
        )
        trigger = "at-limit" if exhausted else "proactive"
        if self.dry_run:
            self._event("switch", trigger=trigger, dryRun=True,
                        **{"from": _ref(active), "to": _ref(target)})
            return "switched"
        before, after = self.store.switch(target.number)
        self._event("switch", trigger=trigger, dryRun=False,
                    **{"from": _ref(before) if before else None, "to": _ref(after)})
        return "switched"

    def _in_cooldown(self, now: float) -> bool:
        last = self.store._read_index().get("lastSwitchAt")
        return isinstance(last, (int, float)) and now - last < self.settings.cooldown_seconds

    def _event(self, kind: str, **fields) -> None:
        self.emit({"schemaVersion": 1, "event": kind, "ts": _iso(self.clock()), **fields})

    def run_loop(self) -> int:
        while not self._stop.is_set():
            try:
                self.tick()
            except ClaudeSwitchError as e:
                self._event("error", message=str(e), transient=True)
            except Exception as e:  # never let one tick kill the loop
                self._event("error", message=f"{type(e).__name__}: {e}", transient=True)
            self._stop.wait(self.settings.interval_seconds)
        return 0

    def stop(self) -> None:
        self._stop.set()


def _ref(account: CodexAccount) -> dict:
    return {"number": int(account.number), "email": account.email}


def auto_service_label() -> str:
    return "com.cswap.codex-auto"


def describe_usage(usage: CodexUsage | None) -> str:
    if usage is None:
        return "not read yet"
    if usage.error:
        return f"unreadable ({usage.error})"
    parts = [f"{w.label} {w.pct:.0f}%" for w in usage.windows]
    if usage.usage_allowed is False:
        parts.append("out of included usage")
    return " · ".join(parts) or "no limits reported"

