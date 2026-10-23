"""Restart Claude Code sessions that stopped on a usage limit.

A session that hits its limit waits for that window's reset ("Continuing
automatically at 12:10am") and never notices that cswap switched the login,
so it stays stopped until someone types into it. Measured 10-22: a fresh
Team seat was active from 21:09 ET, yet nine sessions sat on the limit notice
until they were nudged by hand at 21:16-21:21, and one was still waiting at
21:25. One prompt resumes a session on the new login.

Herdr (https://herdr.dev) hosts each session as an agent pane it can read
and prompt, so after a switch the daemon prompts every Claude pane whose
status area still shows the notice. Without Herdr this does nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

# The banner Claude Code shows under the prompt while a session waits for its
# limit to reset. It goes away once the session runs again; the same words
# can linger in the transcript above the prompt, which is why only the status
# area below the prompt line counts.
STALL_NOTICE = "Usage limit reached"
PROMPT_MARKER = "❯"

# Rounds after a switch. Claude Code picks the new login up from the macOS
# Keychain cache about 30s after the swap, so the first round waits past
# that; the second catches sessions refused while the swap was landing.
ROUND_DELAYS_S = (45.0, 150.0)

PROMPT = "continue"

# Bottom lines of a pane that hold the prompt and its status area.
SCREEN_LINES = 15

COMMAND_TIMEOUT_S = 15.0

# launchd starts the daemon with a bare PATH, so look where Herdr installs.
_HERDR_PATHS = (
    "/opt/homebrew/bin/herdr",
    "/usr/local/bin/herdr",
    "~/.local/bin/herdr",
)

Runner = Callable[[Sequence[str]], str | None]


def find_herdr() -> str | None:
    found = shutil.which("herdr")
    if found:
        return found
    for candidate in _HERDR_PATHS:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _run(args: Sequence[str]) -> str | None:
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def shows_stall_notice(screen: str) -> bool:
    """Whether the status area under the prompt shows the limit banner."""
    lines = screen.splitlines()
    prompt_rows = [
        i for i, line in enumerate(lines) if line.lstrip().startswith(PROMPT_MARKER)
    ]
    if not prompt_rows:
        return False
    return any(STALL_NOTICE in line for line in lines[prompt_rows[-1] + 1 :])


class StalledSessionResumer:
    """Prompts Herdr panes stopped on a usage limit, a few rounds per switch."""

    def __init__(
        self,
        herdr: str,
        *,
        run: Runner = _run,
        sleep: Callable[[float], None] = time.sleep,
        delays: Sequence[float] = ROUND_DELAYS_S,
    ):
        self.herdr = herdr
        self._run = run
        self._sleep = sleep
        self.delays = tuple(delays)
        self._lock = threading.Lock()
        self._episode = 0

    def stalled_agents(self) -> list[dict]:
        listing = self._run([self.herdr, "agent", "list"])
        try:
            agents = json.loads(listing or "")["result"]["agents"]
        except (ValueError, KeyError, TypeError):
            return []
        stalled = []
        for agent in agents if isinstance(agents, list) else []:
            if not isinstance(agent, dict) or agent.get("agent") != "claude":
                continue
            # A working pane is running; a blocked one rejects prompts.
            if agent.get("agent_status") in ("working", "blocked"):
                continue
            pane = agent.get("pane_id")
            if not isinstance(pane, str):
                continue
            screen = self._run([
                self.herdr, "agent", "read", pane,
                "--source", "visible", "--lines", str(SCREEN_LINES),
            ])
            if screen and shows_stall_notice(screen):
                stalled.append(agent)
        return stalled

    def resume_round(self, skip: set[str]) -> list[dict]:
        """Prompt every stalled pane not in ``skip``; return the ones prompted."""
        resumed = []
        for agent in self.stalled_agents():
            pane = agent["pane_id"]
            if pane in skip:
                continue
            if self._run([self.herdr, "agent", "prompt", pane, PROMPT]) is not None:
                resumed.append({
                    "pane": pane,
                    "name": agent.get("name")
                    or agent.get("terminal_title_stripped")
                    or "",
                })
        return resumed

    def after_switch(
        self, report: Callable[[int, list[dict]], None]
    ) -> threading.Thread:
        """Run the resume rounds for one switch on a background thread.

        A newer switch supersedes the rounds still pending for an older one,
        so a pane is never prompted twice for the same stop.
        """
        with self._lock:
            self._episode += 1
            episode = self._episode

        def work() -> None:
            prompted: set[str] = set()
            waited = 0.0
            for number, delay in enumerate(self.delays, start=1):
                self._sleep(max(0.0, delay - waited))
                waited = delay
                if self._episode != episode:
                    return
                resumed = self.resume_round(prompted)
                prompted.update(item["pane"] for item in resumed)
                report(number, resumed)

        thread = threading.Thread(target=work, name="cswap-resume", daemon=True)
        thread.start()
        return thread
