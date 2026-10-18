"""Keep Claude Code's Chrome connection on one account (``cswap chrome``).

The Claude in Chrome extension only talks to a Claude Code session signed into
the same claude.ai account as the extension, and the built-in Chrome tools
connect as the session's account. Every account switch therefore cuts Chrome
off. Claude Code can also run those tools as a standalone MCP server
(``claude --claude-in-chrome-mcp``) that connects with its OWN sign-in, so
this module signs a dedicated config folder into the extension's account and
registers that server for every session. The session's account can then
change freely while the browser connection stays put.

The pinned folder holds an independent sign-in made with ``claude auth
login``, never a copy of a credential cswap manages: two copies of one token
family refresh against each other, and the copy that loses is a dead login.

Edits to Claude Code's global config go through ``claude mcp add/remove`` and,
for the one key this module writes itself, through Claude Code's own config
lock (see ``claude_locks``), so a running session's write is never lost.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_swap import paths
from claude_swap.claude_locks import claude_config_lock
from claude_swap.settings import atomic_write_json

SERVER_FLAG = "--claude-in-chrome-mcp"
RESERVED_NAME = "claude-in-chrome"
DEFAULT_KEY = "claudeInChromeDefaultEnabled"
PRIOR_FILENAME = "prior-default"
# Credentials in the environment override a config folder's sign-in, so the
# pinned server, and every check run on it, starts without them.
STRIPPED_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

Runner = Callable[..., subprocess.CompletedProcess]


class ChromePinError(Exception):
    """A setup step refused or failed; the message is for the user."""


@dataclass(frozen=True)
class Account:
    email: str
    org: str

    def describe(self) -> str:
        return f"{self.email} ({self.org})" if self.org else self.email


def _default_runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, **kwargs)


class ChromePin:
    """One pinned Chrome connection: its sign-in folder and its MCP entry."""

    def __init__(
        self,
        pin_dir: Path | None = None,
        server_name: str | None = None,
        config_path: Path | None = None,
        claude_bin: str | None = None,
        runner: Runner = _default_runner,
        lock=claude_config_lock,
    ):
        self.pin_dir = Path(
            pin_dir or os.environ.get("CSWAP_CHROME_DIR") or Path.home() / ".claude-chrome"
        ).expanduser()
        self.server_name = server_name or os.environ.get("CSWAP_CHROME_NAME") or "chrome"
        self.config_path = config_path or paths.get_global_config_path()
        self.claude_bin = claude_bin or shutil.which("claude") or "claude"
        self.run = runner
        self.lock = lock
        self.prior_file = self.pin_dir / PRIOR_FILENAME

    # -- environment -------------------------------------------------------

    def pinned_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        env["CLAUDE_CONFIG_DIR"] = str(self.pin_dir)
        return env

    def guard_pin_dir(self) -> None:
        pin = os.path.realpath(self.pin_dir)
        home = os.path.realpath(Path.home())
        forbidden = {home, os.path.join(home, ".claude")}
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            forbidden.add(os.path.realpath(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()))
        if pin in forbidden:
            raise ChromePinError(
                "The Chrome sign-in folder must be a folder of its own, "
                "not your home or main Claude folder (CSWAP_CHROME_DIR)."
            )

    # -- accounts ----------------------------------------------------------

    def _account(self, env: dict[str, str]) -> Account | None:
        out = self.run(
            [self.claude_bin, "auth", "status"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        try:
            data = json.loads(out.stdout or "")
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("loggedIn"):
            return None
        return Account(data.get("email") or "unknown", data.get("orgName") or "")

    def pinned_account(self) -> Account | None:
        return self._account(self.pinned_env())

    def session_account(self) -> Account | None:
        return self._account(dict(os.environ))

    # -- Claude Code's global config ---------------------------------------

    def _load_config(self) -> dict | None:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeDecodeError) as e:
            raise ChromePinError(
                f"{self.config_path} could not be read as JSON ({e}). "
                "A running Claude Code session may be writing it; try again."
            ) from e
        return data if isinstance(data, dict) else None

    def server_state(self) -> str:
        """``none``, ``ours``, ``ours-stale`` (ours, older form) or ``other``."""
        server = ((self._load_config() or {}).get("mcpServers") or {}).get(self.server_name)
        if not server:
            return "none"
        args = server.get("args") or []
        env = server.get("env") or {}
        configured = env.get("CLAUDE_CONFIG_DIR", "")
        same_dir = bool(configured) and os.path.realpath(
            Path(configured).expanduser()
        ) == os.path.realpath(self.pin_dir)
        if SERVER_FLAG not in args or not same_dir:
            return "other"
        current = (
            server.get("command") == "/usr/bin/env"
            and self.claude_bin in args
            and all(name in args for name in STRIPPED_ENV)
        )
        return "ours" if current else "ours-stale"

    def builtin_default(self) -> str:
        """``true``, ``false``, ``unset`` or ``unknown`` (no config file)."""
        data = self._load_config()
        if data is None:
            return "unknown"
        value = data.get(DEFAULT_KEY)
        return "unset" if value is None else str(bool(value)).lower()

    def set_builtin_default(self, value: str) -> bool:
        """Write ``true``/``false``, or remove the key for ``unset``.

        Returns False when there is no config file to write.
        """
        with self.lock():
            data = self._load_config()
            if data is None:
                return False
            if value == "unset":
                if DEFAULT_KEY not in data:
                    return True
                del data[DEFAULT_KEY]
            else:
                wanted = value == "true"
                if data.get(DEFAULT_KEY) is wanted:
                    return True
                data[DEFAULT_KEY] = wanted
            atomic_write_json(self.config_path, data)
        return True

    def _mcp(self, *args: str) -> None:
        out = self.run(
            [self.claude_bin, "mcp", *args],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            detail = (out.stderr or out.stdout or "").strip().splitlines()
            raise ChromePinError(
                f"claude mcp {args[0]} failed: {detail[-1] if detail else out.returncode}"
            )

    # -- commands ----------------------------------------------------------

    def setup(self, *, relogin: bool = False, force: bool = False, say=print) -> list[dict] | None:
        if self.server_name == RESERVED_NAME:
            raise ChromePinError(f"'{RESERVED_NAME}' is a reserved server name; pick another")
        self.guard_pin_dir()
        state = self.server_state()
        if state == "other" and not force:
            raise ChromePinError(
                f"A different MCP server is already named '{self.server_name}'. "
                "Set CSWAP_CHROME_NAME to another name, or rerun with --force to replace it."
            )

        self.pin_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.pin_dir, 0o700)

        account = self.pinned_account()
        if relogin or account is None:
            say("Sign in for the Chrome connection. Approve it as the SAME claude.ai account your")
            say("Chrome extension is signed into. The sign-in page uses whichever claude.ai account")
            say("your browser is currently logged into.")
            self.run([self.claude_bin, "auth", "login", "--claudeai"], env=self.pinned_env())
            account = self.pinned_account()
            if account is None:
                raise ChromePinError("The sign-in did not complete.")
        say(f"Chrome connection account: {account.describe()}")

        session = self.session_account()
        if session is not None and session.org != account.org:
            say("")
            say(f"Note: your sessions are signed into {session.describe()}, a different organization.")
            say("The pinned account's organization decides whether Chrome is allowed. If your session's")
            say("organization has turned off Claude in Chrome, don't use this to get around that.")
            say("")

        if state in ("ours-stale", "other"):
            self._mcp("remove", "--scope", "user", self.server_name)
        if state != "ours":
            strip = [a for name in STRIPPED_ENV for a in ("-u", name)]
            self._mcp(
                "add", "--scope", "user", self.server_name,
                "-e", f"CLAUDE_CONFIG_DIR={self.pin_dir}",
                "--", "/usr/bin/env", *strip, self.claude_bin, SERVER_FLAG,
            )
            say(f"Added MCP server '{self.server_name}' to this Claude Code configuration.")
        else:
            say(f"MCP server '{self.server_name}' is already set up.")
        if self.server_state() != "ours":
            raise ChromePinError("The MCP server was not saved as expected.")

        if not self.prior_file.exists():
            self.prior_file.write_text(self.builtin_default() + "\n", encoding="utf-8")
        if not self.set_builtin_default("false"):
            say("Could not find Claude Code's config; turn the built-in Chrome tools off with /chrome.")
        else:
            say("Built-in Chrome tools are off by default, so sessions load only the pinned ones.")
        return self.test(say=say)

    def undo(self, *, keep_login: bool = False, say=print) -> None:
        self.guard_pin_dir()
        state = self.server_state()
        if state in ("ours", "ours-stale"):
            self._mcp("remove", "--scope", "user", self.server_name)
            say(f"Removed MCP server '{self.server_name}'.")
        elif state == "other":
            say(f"Left '{self.server_name}' alone: it is not the server cswap set up.")

        prior = (
            self.prior_file.read_text(encoding="utf-8").strip()
            if self.prior_file.exists() else ""
        )
        if prior in ("true", "false", "unset"):
            self.set_builtin_default(prior)
            self.prior_file.unlink()
            say(f"Built-in Chrome default restored to its earlier setting ({prior}).")
        else:
            say("Left the built-in Chrome default as it is (no earlier setting was saved).")

        if not keep_login and self.pinned_account() is not None:
            self.run(
                [self.claude_bin, "auth", "logout"],
                env=self.pinned_env(), capture_output=True, text=True, timeout=60,
            )
            say("Signed out the pinned account.")
        say(f"The folder {self.pin_dir} is left in place; delete it yourself if you no longer need it.")

    def test(self, *, say=print, settle_s: float = 8.0) -> list[dict] | None:
        """Start the server as the pinned account; list the browsers it reaches."""
        say("Starting the Chrome server as the pinned account and asking which browsers it can reach...")
        browsers = probe_browsers(self.claude_bin, self.pinned_env(), settle_s=settle_s)
        if browsers is None:
            say("The Chrome server did not answer. Your Claude Code version may not support this.")
        elif browsers:
            say(f"OK: {len(browsers)} browser(s) reachable:")
            for b in browsers:
                say(f"  {b.get('name')}  ({b.get('deviceId')})")
            if len(browsers) > 1:
                say("Several browsers are connected; Claude asks you to pick one the first time.")
        else:
            say("No browser is reachable as this account.")
            say("Check that Chrome is open with the Claude extension, and that the extension is")
            say("signed in to the same claude.ai account as the pinned sign-in (cswap chrome status).")
            say("To change the pinned account: cswap chrome setup --relogin")
        return browsers


def probe_browsers(claude_bin: str, env: dict[str, str], *, settle_s: float = 8.0) -> list[dict] | None:
    """Speak just enough MCP over stdio to call ``list_connected_browsers``.

    Returns the browser list, ``[]`` when none is reachable, or ``None`` when
    the server never answered.
    """
    proc = subprocess.Popen(
        [claude_bin, SERVER_FLAG],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, env=env,
    )
    replies: dict[int, dict] = {}

    def read() -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and "id" in msg:
                replies[msg["id"]] = msg

    threading.Thread(target=read, daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def call(i: int, method: str, params: dict, timeout: float) -> dict | None:
        send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end and i not in replies:
            time.sleep(0.2)
        return replies.get(i)

    try:
        init = call(1, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "cswap-chrome", "version": "0"},
        }, 30)
        if not init:
            return None
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(settle_s)  # the bridge connects in the background
        res = call(2, "tools/call", {"name": "list_connected_browsers", "arguments": {}}, 40)
        if not res:
            return None
        text = " ".join(c.get("text", "") for c in res.get("result", {}).get("content", []))
        try:
            browsers, _ = json.JSONDecoder().raw_decode(text.strip())
        except ValueError:
            return []
        return browsers if isinstance(browsers, list) else []
    except (BrokenPipeError, OSError):
        return None
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
