"""Tests for Codex login rotation (codex.py)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from claude_swap import launch_agent
from claude_swap.codex import (
    CodexAutoEngine,
    CodexAutoSettings,
    CodexError,
    CodexStore,
    CodexUsage,
    CodexWindow,
    parse_rate_limits,
    read_identity,
)


def _jwt(claims: dict) -> str:
    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
        return raw.rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def _auth(email: str, account_id: str, user_id: str, plan: str, refresh: str) -> str:
    claims = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "chatgpt_plan_type": plan,
        },
    }
    return json.dumps({
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": "2026-10-19T22:11:37Z",
        "tokens": {
            "id_token": _jwt(claims),
            "access_token": "at-" + refresh,
            "refresh_token": refresh,
            "account_id": account_id,
        },
    })


PRO = ("kaya@example.com", "acct-personal", "user-kaya", "pro")
SEAT_OPS = ("ops@example.com", "acct-team", "user-ops", "team")
SEAT_HELLO = ("hello@example.com", "acct-team", "user-hello", "team")


def _usage(pct5: float | None, pct7: float, *, allowed: bool | None = True, error=None):
    windows = []
    if pct5 is not None:
        windows.append(CodexWindow("5h", pct5, 2_000_000.0))
    windows.append(CodexWindow("7d", pct7, 2_500_000.0))
    return CodexUsage(tuple(windows), allowed, False, 1_000_000.0, error=error)


class Harness:
    def __init__(self, tmp_path: Path):
        self.backup = tmp_path / "backup"
        self.live = tmp_path / "codex-home"
        self.live.mkdir()
        self.logins: list[tuple] = []
        self.readings: dict[str, CodexUsage] = {}
        self.store = CodexStore(
            self.backup,
            live_home=self.live,
            codex="/bin/false",
            usage_reader=self._read,
            login_runner=self._login,
        )

    def _read(self, home: Path) -> CodexUsage:
        identity = read_identity(home / "auth.json")
        return self.readings.get(identity.email if identity else "", _usage(None, 0, error="unread"))

    def _login(self, home: Path) -> int:
        who = self.logins.pop(0)
        (home / "auth.json").write_text(_auth(*who, refresh=f"rt-{who[0]}-{len(self.logins)}"))
        return 0

    def write_live(self, who, refresh="rt-live") -> None:
        (self.live / "auth.json").write_text(_auth(*who, refresh=refresh))


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


class TestStore:
    def test_adopt_links_the_live_login_without_a_second_copy(self, h):
        h.write_live(PRO)
        inode = (h.live / "auth.json").stat().st_ino
        account = h.store.adopt_live()
        assert (account.number, account.email, account.plan) == ("1", "kaya@example.com", "pro")
        assert account.active
        live = h.live / "auth.json"
        assert live.is_symlink()
        assert live.resolve() == h.store.slot_auth("1").resolve()
        assert h.store.slot_auth("1").stat().st_ino == inode
        assert oct(h.store.slot_auth("1").stat().st_mode & 0o777) == "0o600"
        assert h.store.adopt_live().number == "1"

    def test_add_stores_a_new_login_and_leaves_the_live_one(self, h):
        h.write_live(PRO)
        h.logins.append(SEAT_OPS)
        account = h.store.add_login()
        assert (account.number, account.email, account.active) == ("2", "ops@example.com", False)
        assert h.store.active_number() == "1"
        assert not list(h.store.slots.glob(".login-*"))

    def test_logging_in_a_stored_account_again_replaces_it(self, h):
        h.write_live(PRO)
        h.logins += [SEAT_OPS, SEAT_OPS]
        first = h.store.add_login()
        again = h.store.add_login()
        assert first.number == again.number == "2"
        assert len(h.store.accounts()) == 2

    def test_same_email_in_another_workspace_is_a_separate_account(self, h):
        h.write_live(PRO)
        h.logins.append(("kaya@example.com", "acct-team", "user-kaya", "team"))
        assert h.store.add_login().number == "2"

    def test_failed_login_adds_nothing(self, h):
        h.store._login_runner = lambda home: 1
        with pytest.raises(CodexError):
            h.store.add_login()
        assert h.store.accounts() == []

    def test_switch_repoints_the_live_login(self, h):
        h.write_live(PRO)
        h.logins.append(SEAT_OPS)
        h.store.add_login()
        before, after = h.store.switch("ops@example.com")
        assert (before.number, after.number) == ("1", "2")
        assert h.store.active_number() == "2"
        assert read_identity(h.live / "auth.json").email == "ops@example.com"

    def test_a_refresh_through_the_live_path_lands_in_the_active_slot(self, h):
        h.write_live(PRO)
        h.logins.append(SEAT_OPS)
        h.store.add_login()
        h.store.switch("2")
        pro_before = h.store.slot_auth("1").read_text()
        # Codex saves by opening auth.json in place (truncate + write).
        with open(h.live / "auth.json", "w") as f:
            f.write(_auth(*SEAT_OPS, refresh="rt-rotated"))
        assert json.loads(h.store.slot_auth("2").read_text())["tokens"]["refresh_token"] == "rt-rotated"
        assert h.store.slot_auth("1").read_text() == pro_before

    def test_switch_first_stores_an_unmanaged_live_login(self, h):
        h.write_live(PRO)
        h.store.adopt_live()
        h.logins.append(SEAT_OPS)
        h.store.add_login()
        # `codex logout` then `codex login` leaves a plain file.
        (h.live / "auth.json").unlink()
        h.write_live(SEAT_HELLO, refresh="rt-hello")
        h.store.switch("1")
        emails = {a.email for a in h.store.accounts()}
        assert emails == {"kaya@example.com", "ops@example.com", "hello@example.com"}
        assert h.store.active_number() == "1"

    def test_a_plain_codex_login_through_the_link_is_relabelled(self, h):
        h.write_live(PRO)
        h.store.adopt_live()
        with open(h.live / "auth.json", "w") as f:
            f.write(_auth(*SEAT_HELLO, refresh="rt-x"))
        notes = h.store.check_slots()
        assert notes and "hello@example.com" in notes[0]
        assert h.store.resolve("1").email == "hello@example.com"

    def test_remove_refuses_the_active_login(self, h):
        h.write_live(PRO)
        h.logins.append(SEAT_OPS)
        h.store.add_login()
        with pytest.raises(CodexError):
            h.store.remove("1")
        h.store.remove("2")
        assert [a.number for a in h.store.accounts()] == ["1"]
        assert not h.store.slot_dir("2").exists()

    def test_unknown_target(self, h):
        with pytest.raises(CodexError):
            h.store.resolve("9")

    def test_failed_read_keeps_the_last_good_reading(self, h):
        h.write_live(PRO)
        h.store.adopt_live()
        h.readings["kaya@example.com"] = _usage(None, 40)
        h.store.refresh_usage()
        h.readings["kaya@example.com"] = _usage(None, 0, error="timeout")
        h.store.refresh_usage()
        assert h.store.resolve("1").usage.binding_pct == 40


class TestRateLimits:
    def test_team_seat_with_both_windows(self):
        usage = parse_rate_limits({
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "primary": {"usedPercent": 67, "windowDurationMins": 300, "resetsAt": 1_792_730_000},
                "secondary": {"usedPercent": 26, "windowDurationMins": 10080, "resetsAt": 1_793_000_000},
            },
        }, 1.0)
        assert [(w.label, w.pct) for w in usage.windows] == [("5h", 67.0), ("7d", 26.0)]
        assert usage.binding_pct == 67.0
        assert usage.weekly_reset == 1_793_000_000

    def test_named_model_limits_do_not_bind(self):
        usage = parse_rate_limits({
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 10, "windowDurationMins": 10080}},
                "spark": {
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": {"usedPercent": 99, "windowDurationMins": 300},
                },
            },
        }, 1.0)
        assert usage.binding_pct == 10.0
        assert ("Spark 5h", 99.0) in [(w.label, w.pct) for w in usage.windows]

    def test_backend_verdict_overrides_percentages(self):
        usage = parse_rate_limits({
            "ordinaryUsageAllowed": False,
            "rateLimits": {"primary": {"usedPercent": 80, "windowDurationMins": 10080}},
        }, 1.0)
        assert usage.binding_pct == 100.0

    def test_round_trip_through_the_cache(self):
        usage = _usage(30, 50)
        again = CodexUsage.from_json(usage.to_json())
        assert again.binding_pct == 50
        assert [w.label for w in again.windows] == ["5h", "7d"]


class TestAuto:
    def _setup(self, h, live_pct, ops_pct, hello_pct):
        h.write_live(PRO)
        h.logins += [SEAT_OPS, SEAT_HELLO]
        h.store.add_login()
        h.store.add_login()
        h.readings.update({
            "kaya@example.com": live_pct,
            "ops@example.com": ops_pct,
            "hello@example.com": hello_pct,
        })
        events: list[dict] = []
        clock = [1_000_000.0]
        engine = CodexAutoEngine(h.store, CodexAutoSettings(threshold=90), events.append,
                                 clock=lambda: clock[0])
        return engine, events, clock

    def test_below_threshold_stays(self, h):
        engine, events, _ = self._setup(h, _usage(None, 40), _usage(10, 10), _usage(5, 5))
        assert engine.tick() == "no-action"
        assert h.store.active_number() == "1"

    def test_moves_to_the_most_room_at_the_threshold(self, h):
        engine, events, _ = self._setup(h, _usage(None, 92), _usage(40, 10), _usage(5, 20))
        assert engine.tick() == "switched"
        assert h.store.active_number() == "3"
        switch = next(e for e in events if e["event"] == "switch")
        assert switch["trigger"] == "proactive"
        assert switch["to"]["email"] == "hello@example.com"

    def test_cooldown_holds_a_proactive_move_but_not_an_exhausted_login(self, h):
        engine, events, clock = self._setup(h, _usage(None, 92), _usage(40, 10), _usage(5, 20))
        engine.tick()
        h.readings["hello@example.com"] = _usage(93, 30)
        clock[0] += 60
        assert engine.tick() == "no-action"
        assert events[-1]["reason"] == "cooldown"
        h.readings["hello@example.com"] = _usage(100, 30, allowed=False)
        clock[0] += 60
        assert engine.tick() == "switched"
        assert h.store.active_number() == "2"

    def test_nothing_with_room_blocks(self, h):
        engine, events, _ = self._setup(h, _usage(None, 100, allowed=False), _usage(95, 10), _usage(None, 0, error="x"))
        assert engine.tick() == "blocked"
        assert h.store.active_number() == "1"

    def test_dry_run_never_switches(self, h):
        engine, events, _ = self._setup(h, _usage(None, 95), _usage(10, 10), _usage(50, 50))
        engine.dry_run = True
        assert engine.tick() == "switched"
        assert h.store.active_number() == "1"

    def test_an_unmanaged_live_login_is_left_alone(self, h):
        h.write_live(PRO)
        events: list[dict] = []
        engine = CodexAutoEngine(h.store, CodexAutoSettings(), events.append)
        h.logins.append(SEAT_OPS)
        h.store.add_login()
        (h.live / "auth.json").unlink()
        h.write_live(SEAT_HELLO)
        assert engine.tick() == "no-action"
        assert events[-1]["reason"] == "unmanaged-login"


def test_launch_agent_runs_any_subcommand(tmp_path):
    plist = launch_agent.build_plist(
        ["/x/cswap"], label="com.cswap.codex-auto", home=tmp_path,
        arguments=("codex", "auto", "--json"),
    )
    import plistlib

    data = plistlib.loads(plist)
    assert data["ProgramArguments"] == ["/x/cswap", "codex", "auto", "--json"]
    assert data["StandardOutPath"].endswith("com.cswap.codex-auto.log")
    assert plistlib.loads(launch_agent.build_plist(["/x/cswap"], home=tmp_path))[
        "ProgramArguments"
    ] == ["/x/cswap", "menubar"]


class TestRotatingHome:
    def _two_homes(self, h, tmp_path):
        h.write_live(PRO)
        h.store.adopt_live()  # ~/.codex analogue keeps Pro
        daybreak = tmp_path / "codex-daybreak"
        daybreak.mkdir()
        (daybreak / "auth.json").write_text(_auth(*SEAT_OPS, refresh="rt-db"))
        return daybreak

    def test_moving_the_rotation_pins_the_old_home(self, h, tmp_path):
        daybreak = self._two_homes(h, tmp_path)
        account = h.store.set_live_home(daybreak)
        assert (account.number, account.email) == ("2", "ops@example.com")
        assert (daybreak / "auth.json").is_symlink()
        assert h.store.live_home == daybreak
        pro = h.store.resolve("1")
        assert pro.pinned_to == (str(h.live),)
        assert not pro.active
        # The old home still runs Pro through its own link.
        assert read_identity(h.live / "auth.json").email == "kaya@example.com"

    def test_a_pinned_login_is_never_rotated_in_or_removed(self, h, tmp_path):
        daybreak = self._two_homes(h, tmp_path)
        h.store.set_live_home(daybreak)
        with pytest.raises(CodexError):
            h.store.switch("1")
        with pytest.raises(CodexError):
            h.store.remove("1")
        h.logins.append(SEAT_HELLO)
        h.store.add_login()
        h.readings.update({
            "ops@example.com": _usage(95, 10),
            "kaya@example.com": _usage(None, 1),
            "hello@example.com": _usage(10, 10),
        })
        events: list[dict] = []
        engine = CodexAutoEngine(h.store, CodexAutoSettings(threshold=90), events.append)
        assert engine.tick() == "switched"
        assert h.store.active_number() == "3"

    def test_switches_are_logged_and_the_log_resets_with_the_home(self, h, tmp_path):
        h.write_live(PRO)
        h.logins.append(SEAT_OPS)
        h.store.add_login()
        h.store.switch("2")
        log = h.store._read_index()["switchLog"]
        assert [(e["from"], e["to"]) for e in log] == [("1", "2")]
        daybreak = tmp_path / "db"
        daybreak.mkdir()
        h.store.set_live_home(daybreak)
        assert h.store._read_index()["switchLog"] == []
