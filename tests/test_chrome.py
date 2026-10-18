"""Tests for the pinned Chrome connection (claude_swap.chrome)."""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from claude_swap import chrome
from claude_swap.chrome import STRIPPED_ENV, ChromePin, ChromePinError

BIN = "/opt/bin/claude"


class FakeClaude:
    """Stands in for `claude auth ...` and `claude mcp add/remove`."""

    def __init__(self, config: Path, pin_dir: Path, *, pinned_org="Team", session_org="Team"):
        self.config = config
        self.pin_dir = pin_dir
        self.pinned_logged_in = False
        self.pinned_org = pinned_org
        self.session_org = session_org
        self.calls: list[tuple[list[str], dict | None]] = []

    def _data(self) -> dict:
        return json.loads(self.config.read_text())

    def _save(self, data: dict) -> None:
        self.config.write_text(json.dumps(data))

    def __call__(self, argv, env=None, **kwargs):
        self.calls.append((list(argv), env))
        sub = argv[1:]
        out = ""
        if sub[:2] == ["auth", "status"]:
            if (env or {}).get("CLAUDE_CONFIG_DIR") == str(self.pin_dir):
                data = {"loggedIn": self.pinned_logged_in, "email": "pin@example.com",
                        "orgName": self.pinned_org}
            else:
                data = {"loggedIn": True, "email": "me@example.com", "orgName": self.session_org}
            out = json.dumps(data)
        elif sub[:2] == ["auth", "login"]:
            self.pinned_logged_in = True
        elif sub[:2] == ["auth", "logout"]:
            self.pinned_logged_in = False
        elif sub[:2] == ["mcp", "add"]:
            name = sub[4]
            rest = sub[5:]
            env_vars = {}
            while rest[0] == "-e":
                key, value = rest[1].split("=", 1)
                env_vars[key] = value
                rest = rest[2:]
            assert rest[0] == "--"
            data = self._data()
            data.setdefault("mcpServers", {})[name] = {
                "type": "stdio", "command": rest[1], "args": rest[2:], "env": env_vars,
            }
            self._save(data)
        elif sub[:2] == ["mcp", "remove"]:
            data = self._data()
            data["mcpServers"].pop(sub[4])
            self._save(data)
        return subprocess.CompletedProcess(argv, 0, out, "")

    def made(self, *prefix: str) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[1:1 + len(prefix)] == list(prefix)]


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"claudeInChromeDefaultEnabled": True, "mcpServers": {}}))
    pin_dir = tmp_path / "pin"
    fake = FakeClaude(config, pin_dir)
    pin = ChromePin(
        pin_dir=pin_dir, server_name="chrome", config_path=config, claude_bin=BIN,
        runner=fake, lock=contextlib.nullcontext,
    )
    browsers = [{"deviceId": "dev-1", "name": "Browser 1"}]
    monkeypatch.setattr(chrome, "probe_browsers", lambda *a, **k: browsers)
    return pin, fake, config


def _quiet(*_args, **_kwargs):
    pass


def test_setup_signs_in_registers_and_turns_off_the_builtin(setup_env, monkeypatch):
    pin, fake, config = setup_env
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "should-not-leak")

    browsers = pin.setup(say=_quiet)

    assert browsers == [{"deviceId": "dev-1", "name": "Browser 1"}]
    login_env = next(env for argv, env in fake.calls if argv[1:3] == ["auth", "login"])
    assert login_env["CLAUDE_CONFIG_DIR"] == str(pin.pin_dir)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in login_env
    server = json.loads(config.read_text())["mcpServers"]["chrome"]
    assert server["command"] == "/usr/bin/env"
    assert server["args"][-2:] == [BIN, "--claude-in-chrome-mcp"]
    for name in STRIPPED_ENV:
        assert name in server["args"]
    assert server["env"] == {"CLAUDE_CONFIG_DIR": str(pin.pin_dir)}
    assert pin.server_state() == "ours"
    assert pin.builtin_default() == "false"
    assert pin.prior_file.read_text().strip() == "true"


def test_setup_again_changes_nothing(setup_env):
    pin, fake, _ = setup_env
    pin.setup(say=_quiet)
    adds = len(fake.made("mcp", "add"))
    logins = len(fake.made("auth", "login"))

    pin.setup(say=_quiet)

    assert len(fake.made("mcp", "add")) == adds
    assert len(fake.made("auth", "login")) == logins
    assert pin.prior_file.read_text().strip() == "true"


def test_setup_refuses_a_different_server_with_the_name(setup_env):
    pin, fake, config = setup_env
    data = json.loads(config.read_text())
    data["mcpServers"]["chrome"] = {"type": "stdio", "command": "npx", "args": ["chrome-devtools-mcp"]}
    config.write_text(json.dumps(data))

    with pytest.raises(ChromePinError, match="already named"):
        pin.setup(say=_quiet)

    assert json.loads(config.read_text())["mcpServers"]["chrome"]["command"] == "npx"
    assert pin.builtin_default() == "true"
    assert fake.made("auth", "login") == []


def test_force_replaces_a_different_server(setup_env):
    pin, _, _ = setup_env
    data = json.loads(pin.config_path.read_text())
    data["mcpServers"]["chrome"] = {"type": "stdio", "command": "npx", "args": []}
    pin.config_path.write_text(json.dumps(data))

    pin.setup(force=True, say=_quiet)

    assert pin.server_state() == "ours"


def test_an_older_entry_of_ours_is_upgraded(setup_env):
    pin, fake, config = setup_env
    pin.pin_dir.mkdir()
    fake.pinned_logged_in = True
    data = json.loads(config.read_text())
    data["mcpServers"]["chrome"] = {
        "type": "stdio", "command": BIN, "args": ["--claude-in-chrome-mcp"],
        "env": {"CLAUDE_CONFIG_DIR": str(pin.pin_dir)},
    }
    config.write_text(json.dumps(data))
    assert pin.server_state() == "ours-stale"

    pin.setup(say=_quiet)

    assert pin.server_state() == "ours"
    assert len(fake.made("mcp", "remove")) == 1


def test_undo_restores_the_earlier_default_and_signs_out(setup_env):
    pin, fake, config = setup_env
    pin.setup(say=_quiet)

    pin.undo(say=_quiet)

    assert "chrome" not in json.loads(config.read_text())["mcpServers"]
    assert pin.builtin_default() == "true"
    assert not pin.prior_file.exists()
    assert fake.pinned_logged_in is False


def test_undo_restores_an_unset_default_by_removing_the_key(setup_env):
    pin, _, config = setup_env
    config.write_text(json.dumps({"mcpServers": {}}))
    pin.setup(say=_quiet)
    assert pin.builtin_default() == "false"

    pin.undo(keep_login=True, say=_quiet)

    assert "claudeInChromeDefaultEnabled" not in json.loads(config.read_text())


def test_undo_leaves_someone_elses_server_and_the_default_alone(setup_env):
    pin, fake, config = setup_env
    data = json.loads(config.read_text())
    data["mcpServers"]["chrome"] = {"type": "stdio", "command": "npx", "args": []}
    config.write_text(json.dumps(data))

    pin.undo(say=_quiet)

    after = json.loads(config.read_text())
    assert after["mcpServers"]["chrome"]["command"] == "npx"
    assert after["claudeInChromeDefaultEnabled"] is True
    assert fake.made("mcp", "remove") == []


def test_reserved_name_and_home_folder_are_refused(setup_env, tmp_path):
    pin, _, _ = setup_env
    reserved = ChromePin(
        pin_dir=tmp_path / "p2", server_name="claude-in-chrome", config_path=pin.config_path,
        claude_bin=BIN, runner=pin.run, lock=contextlib.nullcontext,
    )
    with pytest.raises(ChromePinError, match="reserved"):
        reserved.setup(say=_quiet)

    home = ChromePin(
        pin_dir=Path.home(), config_path=pin.config_path, claude_bin=BIN,
        runner=pin.run, lock=contextlib.nullcontext,
    )
    with pytest.raises(ChromePinError, match="folder of its own"):
        home.setup(say=_quiet)


def test_a_different_session_org_is_pointed_out(setup_env):
    pin, fake, _ = setup_env
    fake.session_org = "Other Org"
    lines: list[str] = []

    pin.setup(say=lines.append)

    assert any("different organization" in line for line in lines)


def test_unreadable_config_is_a_clear_error(setup_env):
    pin, _, config = setup_env
    config.write_text("{not json")

    with pytest.raises(ChromePinError, match="could not be read"):
        pin.server_state()
