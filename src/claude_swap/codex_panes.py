"""Move running Codex sessions onto the login cswap switched to.

A Codex session keeps the login it started with. It reads auth.json again
only when it restarts, and it refuses to reload a login that belongs to
another account (codex-rs ``reload_if_account_id_matches``). So a switch
alone leaves every running session on the old login, and one stopped on that
login's usage limit stays stopped.

Codex can also run a shared app-server daemon per home, and TUIs connected to
it run their turns under the daemon's login. A daemon that started on another
login is restarted (``codex app-server daemon restart``) once no session on
that home is mid-turn, and then every TUI on the home is restarted with it.

After a switch, each Codex agent in Herdr that runs on the rotating home and
started on a different login is restarted into the same conversation
(``codex resume <id>``): at once when it is stopped on a usage limit (with
"continue", so it carries on), otherwise once it has sat idle between turns
for a while. A busy agent is never touched. An orchestrator that drives an
agent itself (the security queue's ``sec-scanner``) calls
``cswap codex sync --agent NAME`` before each new task instead, so a restart
never lands between its prompts.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from claude_swap.codex import CodexStore
from claude_swap.locking import FileLock

# Codex 0.163 shows "Usage limit reached. You've reached your usage limit."
STALL_RE = re.compile(r"usage limit reached|reached your usage limit|hit your usage limit", re.I)

# Idle this long between turns, a pane is safe to restart under its user.
RESTART_IDLE_S = 90.0
QUIT_TIMEOUT_S = 20.0
START_TIMEOUT_S = 90.0
COMMAND_TIMEOUT_S = 20.0
SCREEN_LINES = 30

Runner = Callable[[Sequence[str]], str | None]


def _run(args: Sequence[str]) -> str | None:
    try:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


@dataclass
class CodexDaemon:
    pid: int
    started_at: float


@dataclass
class CodexPane:
    pane_id: str
    name: str | None
    status: str
    pid: int
    argv: list[str]
    cwd: str
    home: Path
    started_at: float

    @property
    def target(self) -> str:
        return self.pane_id


def _process_start(run: Runner, pid: int) -> float | None:
    out = run(["ps", "-o", "lstart=", "-p", str(pid)])
    if not out or not out.strip():
        return None
    try:
        return time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _process_home(run: Runner, pid: int) -> Path:
    out = run(["ps", "eww", "-o", "command=", "-p", str(pid)]) or ""
    for token in out.split():
        if token.startswith("CODEX_HOME="):
            return Path(token.split("=", 1)[1]).expanduser()
    return Path.home() / ".codex"


def codex_args(argv: Sequence[str]) -> list[str]:
    """The flags a Codex pane was started with, minus any earlier
    ``resume <id> [prompt]`` so a restart does not stack them."""
    args = list(argv[1:])
    if "resume" in args:
        args = args[: args.index("resume")]
    return args


def session_id(home: Path, cwd: str, since: float) -> str | None:
    """The conversation a Codex process started at ``since`` in ``cwd`` is in:
    the newest rollout written since then for that folder."""
    best: tuple[float, str] | None = None
    sessions = home / "sessions"
    for path in sessions.rglob("rollout-*.jsonl") if sessions.is_dir() else ():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < since - 5:
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                meta = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        payload = meta.get("payload") if isinstance(meta, dict) else None
        if not isinstance(payload, dict) or payload.get("cwd") != cwd:
            continue
        sid = payload.get("id")
        if isinstance(sid, str) and (best is None or mtime > best[0]):
            best = (mtime, sid)
    return best[1] if best else None


class CodexPaneSync:
    def __init__(
        self,
        store: CodexStore,
        herdr: str,
        *,
        codex: str = "codex",
        run: Runner = _run,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        find_session: Callable[[Path, str, float], str | None] = session_id,
    ):
        self.store = store
        self.herdr = herdr
        self.codex = codex
        self._run = run
        self.clock = clock
        self._sleep = sleep
        self._find_session = find_session
        self._idle_since: dict[str, float] = {}

    # -- discovery ---------------------------------------------------------

    def _agents(self) -> list[dict]:
        out = self._run([self.herdr, "agent", "list"])
        try:
            agents = json.loads(out or "")["result"]["agents"]
        except (ValueError, KeyError, TypeError):
            return []
        return [a for a in agents if isinstance(a, dict) and a.get("agent") == "codex"]

    def _codex_process(self, pane_id: str) -> dict | None:
        out = self._run([self.herdr, "pane", "process-info", "--pane", pane_id])
        try:
            info = json.loads(out or "")["result"]["process_info"]
        except (ValueError, KeyError, TypeError):
            return None
        for proc in info.get("foreground_processes") or []:
            name = str(proc.get("name") or proc.get("argv0") or "")
            if name == "codex" or name.endswith("/codex"):
                return proc
        return None

    def panes(self) -> list[CodexPane]:
        found = []
        for agent in self._agents():
            pane_id = agent.get("pane_id")
            if not isinstance(pane_id, str):
                continue
            proc = self._codex_process(pane_id)
            if proc is None or not isinstance(proc.get("pid"), int):
                continue
            started = _process_start(self._run, proc["pid"])
            if started is None:
                continue
            found.append(
                CodexPane(
                    pane_id=pane_id,
                    name=agent.get("name") or None,
                    status=str(agent.get("agent_status") or "unknown"),
                    pid=proc["pid"],
                    argv=list(proc.get("argv") or ["codex"]),
                    cwd=str(proc.get("cwd") or agent.get("cwd") or ""),
                    home=_process_home(self._run, proc["pid"]),
                    started_at=started,
                )
            )
        return found

    def daemon(self, home: Path) -> CodexDaemon | None:
        """The app-server daemon serving ``home``, if one is running."""
        out = self._run(["env", f"CODEX_HOME={home}", self.codex, "app-server", "daemon", "version"])
        try:
            if json.loads(out or "").get("status") != "running":
                return None
        except (ValueError, AttributeError):
            return None
        listing = self._run(["ps", "-axo", "pid=,command="]) or ""
        for line in listing.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or "app-server --listen" not in parts[1]:
                continue
            pid = int(parts[0])
            if _process_home(self._run, pid).absolute() != home.absolute():
                continue
            started = _process_start(self._run, pid)
            if started is not None:
                return CodexDaemon(pid=pid, started_at=started)
        return None

    # -- decisions ---------------------------------------------------------

    def login_at(self, when: float) -> str | None:
        """The slot the rotating home pointed at, at ``when``."""
        data = self.store._read_index()
        log = [e for e in data.get("switchLog") or [] if isinstance(e, dict)]
        before = [e for e in log if isinstance(e.get("at"), (int, float)) and e["at"] <= when]
        if before:
            return before[-1].get("to")
        if log:
            return log[0].get("from")
        return self.store.active_number()

    def is_stale(self, pane: CodexPane) -> bool:
        if pane.home.absolute() != self.store.live_home.absolute():
            return False
        active = self.store.active_number()
        return active is not None and self.login_at(pane.started_at) != active

    def is_stalled(self, pane: CodexPane) -> bool:
        screen = self._run([
            self.herdr, "agent", "read", pane.target,
            "--source", "visible", "--lines", str(SCREEN_LINES),
        ])
        return bool(screen and STALL_RE.search(screen))

    # -- restart -----------------------------------------------------------

    def restart(self, pane: CodexPane, *, prompt: str | None = None) -> dict:
        sid = self._find_session(pane.home, pane.cwd, pane.started_at)
        result = {"pane": pane.pane_id, "name": pane.name, "session": sid, "continued": bool(prompt)}
        if prompt and sid is None:
            result["error"] = "could not tell which conversation it was in; left as is"
            return result
        self._run([self.herdr, "agent", "prompt", pane.target, "/quit"])
        if not self._wait_for_exit(pane):
            self._run([self.herdr, "pane", "send-keys", pane.pane_id, "ctrl+c"])
            self._run([self.herdr, "pane", "send-keys", pane.pane_id, "ctrl+c"])
            if not self._wait_for_exit(pane):
                result["error"] = "codex did not exit"
                return result
        command = ["env", f"CODEX_HOME={pane.home}", "codex", *codex_args(pane.argv)]
        if sid:
            command += ["resume", sid]
            if prompt:
                command.append(prompt)
        self._run([self.herdr, "pane", "run", pane.pane_id, shlex.join(command)])
        if not self._wait_for_start(pane):
            result["error"] = "codex did not come back in time"
            return result
        if pane.name:
            self._run([self.herdr, "agent", "rename", pane.pane_id, pane.name])
        return result

    def _wait_for_exit(self, pane: CodexPane) -> bool:
        deadline = self.clock() + QUIT_TIMEOUT_S
        while self.clock() < deadline:
            proc = self._codex_process(pane.pane_id)
            if proc is None or proc.get("pid") != pane.pid:
                return True
            self._sleep(1.0)
        return False

    def _wait_for_start(self, pane: CodexPane) -> bool:
        deadline = self.clock() + START_TIMEOUT_S
        while self.clock() < deadline:
            for agent in self._agents():
                if agent.get("pane_id") == pane.pane_id and agent.get("agent_status") in (
                    "idle", "working", "done",
                ):
                    proc = self._codex_process(pane.pane_id)
                    if proc is not None and proc.get("pid") != pane.pid:
                        return True
            self._sleep(2.0)
        return False

    # -- sync --------------------------------------------------------------

    def sync(self, *, agent: str | None = None, min_idle_s: float = RESTART_IDLE_S) -> list[dict]:
        """Restart what still runs on an older login, when it is safe to."""
        with FileLock(self.store.root / ".pane-sync.lock", timeout=300):
            home = self.store.live_home
            active = self.store.active_number()
            panes = [p for p in self.panes() if p.home.absolute() == home.absolute()]
            self._forget_gone(panes)
            if active is None:
                return []
            daemon = self.daemon(home)
            if daemon is not None and self.login_at(daemon.started_at) != active:
                return self._sync_with_daemon(panes, min_idle_s)
            return self._sync_panes(panes, agent, min_idle_s)

    def _sync_panes(self, panes: list[CodexPane], agent: str | None, min_idle_s: float) -> list[dict]:
        done = []
        now = self.clock()
        for pane in panes:
            if agent is not None and agent not in (pane.name, pane.pane_id):
                continue
            if not self.is_stale(pane) or pane.status in ("working", "blocked"):
                self._idle_since.pop(pane.pane_id, None)
                continue
            if self.is_stalled(pane):
                done.append(self.restart(pane, prompt="continue"))
                self._idle_since.pop(pane.pane_id, None)
                continue
            first = self._idle_since.setdefault(pane.pane_id, now)
            if now - first >= min_idle_s:
                done.append(self.restart(pane))
                self._idle_since.pop(pane.pane_id, None)
        return done

    def _sync_with_daemon(self, panes: list[CodexPane], min_idle_s: float) -> list[dict]:
        """Every TUI on the home runs through the daemon: wait until none is
        mid-turn, then restart the daemon and all of them together."""
        now = self.clock()
        if any(p.status in ("working", "blocked") for p in panes):
            for p in panes:
                self._idle_since.pop(p.pane_id, None)
            return []
        stalled = {p.pane_id for p in panes if self.is_stalled(p)}
        quiet = all(
            p.pane_id in stalled or now - self._idle_since.setdefault(p.pane_id, now) >= min_idle_s
            for p in panes
        )
        if not (stalled or quiet):
            return []
        home = self.store.live_home
        self._run(["env", f"CODEX_HOME={home}", self.codex, "app-server", "daemon", "restart"])
        done = [{"daemon": str(home), "restarted": True}]
        for pane in panes:
            done.append(self.restart(pane, prompt="continue" if pane.pane_id in stalled else None))
            self._idle_since.pop(pane.pane_id, None)
        return done

    def _forget_gone(self, panes: list[CodexPane]) -> None:
        live = {p.pane_id for p in panes}
        for pane_id in list(self._idle_since):
            if pane_id not in live:
                self._idle_since.pop(pane_id)
