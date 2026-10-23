"""Tests for resuming Herdr sessions stopped on a usage limit."""

from __future__ import annotations

import json
import threading

from claude_swap import resume
from claude_swap.resume import StalledSessionResumer, shows_stall_notice

# Bottom of a pane waiting on its limit (captured 10-22).
STALLED_SCREEN = """\
  Ran 1 shell command
  ⎿  You've hit your session limit · resets 12:10am (America/New_York)

⏺ Usage limit reached · continuing automatically at 12:10am · esc to cancel

✻ Baked for 6m 14s · done 9:03 PM

────────────────────────────────────────────
❯
────────────────────────────────────────────
  ⚠ Usage limit reached · limit resets 12:10am
    Continuing automatically at 12:10am · esc to cancel
  Sonnet 5.5 medium | context 7% 68k/1m
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent
"""

# The same pane after it ran again: the transcript still mentions the limit,
# the status area does not.
RESUMED_SCREEN = """\
⏺ Usage limit reached · continuing automatically at 12:10am · esc to cancel

❯ continue

⏺ Picking up where I stopped.
────────────────────────────────────────────
❯
────────────────────────────────────────────
  Sonnet 5.5 medium | context 7% 68k/1m
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


def test_status_area_notice_is_a_stall():
    assert shows_stall_notice(STALLED_SCREEN)


def test_notice_above_the_prompt_is_not_a_stall():
    assert not shows_stall_notice(RESUMED_SCREEN)


def test_screen_without_a_prompt_is_not_a_stall():
    assert not shows_stall_notice("⚠ Usage limit reached · limit resets 12:10am\n")


def _agent(pane: str, status: str = "idle", agent: str = "claude", name=None):
    entry = {"agent": agent, "agent_status": status, "pane_id": pane}
    if name:
        entry["name"] = name
    return entry


class FakeHerdr:
    """Answers `herdr agent list|read|prompt` from canned panes."""

    def __init__(self, agents: list[dict], screens: dict[str, str]):
        self.agents = agents
        self.screens = screens
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, args):
        _, group, verb, *rest = args
        assert group == "agent"
        if verb == "list":
            return json.dumps({"result": {"agents": self.agents}})
        if verb == "read":
            return self.screens.get(rest[0], "")
        if verb == "prompt":
            self.prompts.append((rest[0], rest[1]))
            # A prompted pane runs again and its banner clears.
            self.screens[rest[0]] = RESUMED_SCREEN
            return "{}"
        raise AssertionError(args)


def test_round_prompts_only_stalled_claude_panes():
    herdr = FakeHerdr(
        [
            _agent("w1:p1", name="ci-mother"),
            _agent("w1:p2", status="working"),
            _agent("w1:p3", agent="codex"),
            _agent("w1:p4"),
            _agent("w1:p5", status="blocked"),
        ],
        {
            "w1:p1": STALLED_SCREEN,
            "w1:p2": STALLED_SCREEN,
            "w1:p3": STALLED_SCREEN,
            "w1:p4": RESUMED_SCREEN,
            "w1:p5": STALLED_SCREEN,
        },
    )
    resumer = StalledSessionResumer("herdr", run=herdr)
    assert resumer.resume_round(set()) == [{"pane": "w1:p1", "name": "ci-mother"}]
    assert herdr.prompts == [("w1:p1", resume.PROMPT)]


def test_unreadable_listing_resumes_nothing():
    resumer = StalledSessionResumer("herdr", run=lambda args: None)
    assert resumer.resume_round(set()) == []


def test_rounds_after_a_switch_prompt_each_pane_once():
    herdr = FakeHerdr([_agent("w1:p1"), _agent("w1:p2")], {"w1:p1": STALLED_SCREEN})
    sleeps: list[float] = []
    reports: list[tuple[int, list[dict]]] = []
    resumer = StalledSessionResumer(
        "herdr", run=herdr, sleep=sleeps.append, delays=(45.0, 150.0)
    )

    def second_round_stall(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            # Refused while the swap was landing: stalls after round one.
            herdr.screens["w1:p2"] = STALLED_SCREEN

    resumer._sleep = second_round_stall
    resumer.after_switch(lambda n, s: reports.append((n, s))).join(5)
    assert sleeps == [45.0, 105.0]
    assert [(n, [x["pane"] for x in s]) for n, s in reports] == [
        (1, ["w1:p1"]),
        (2, ["w1:p2"]),
    ]


def test_a_newer_switch_supersedes_pending_rounds():
    herdr = FakeHerdr([_agent("w1:p1")], {"w1:p1": STALLED_SCREEN})
    gate = threading.Event()
    reports: list[int] = []
    resumer = StalledSessionResumer(
        "herdr", run=herdr, sleep=lambda s: gate.wait(5), delays=(45.0,)
    )
    first = resumer.after_switch(lambda n, s: reports.append(1))
    resumer._sleep = lambda s: None
    resumer.after_switch(lambda n, s: reports.append(2)).join(5)
    gate.set()
    first.join(5)
    assert reports == [2]
    assert len(herdr.prompts) == 1


def test_find_herdr_checks_install_paths(monkeypatch, tmp_path):
    binary = tmp_path / "herdr"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(resume.shutil, "which", lambda name: None)
    monkeypatch.setattr(resume, "_HERDR_PATHS", (str(tmp_path / "nope"), str(binary)))
    assert resume.find_herdr() == str(binary)
    monkeypatch.setattr(resume, "_HERDR_PATHS", ())
    assert resume.find_herdr() is None
