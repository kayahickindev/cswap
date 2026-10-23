"""Tests for reading usage-limit refusals out of Claude Code transcripts."""

from __future__ import annotations

import json
import os
from pathlib import Path

from claude_swap import limit_signal

NOW = 1_792_717_000.0  # 2026-10-23T01:03:20Z


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _refusal(at: float, resets_at: int = 1_792_728_600) -> dict:
    """A refusal as Claude Code records it (shape copied from a live one)."""
    return {
        "type": "assistant",
        "timestamp": _iso(at),
        "isApiErrorMessage": True,
        "message": {
            "model": "<synthetic>",
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": "You've hit your session limit · resets 12:10am "
                        "(America/New_York)",
            }],
        },
        "apiError": "usage_limit_reached",
        "apiErrorParams": {
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": resets_at,
                "rateLimitType": "five_hour",
            },
        },
    }


def _write(path: Path, records: list[dict], mtime: float = NOW) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    os.utime(path, (mtime, mtime))
    return path


def test_reads_the_refusing_window(tmp_path):
    _write(tmp_path / "proj" / "s1.jsonl", [
        {"type": "user", "timestamp": _iso(NOW - 120), "message": {"content": "hi"}},
        _refusal(NOW - 30),
    ])
    hits = limit_signal.recent_limit_hits(NOW - 300, NOW, root=tmp_path)
    assert len(hits) == 1
    assert hits[0].at == NOW - 30
    assert hits[0].resets_at == 1_792_728_600
    assert hits[0].limit_type == "five_hour"


def test_subagent_transcripts_count(tmp_path):
    _write(
        tmp_path / "proj" / "s1" / "subagents" / "agent-a.jsonl",
        [_refusal(NOW - 10)],
    )
    assert len(limit_signal.recent_limit_hits(NOW - 300, NOW, root=tmp_path)) == 1


def test_refusals_before_since_are_ignored(tmp_path):
    _write(tmp_path / "proj" / "s1.jsonl", [_refusal(NOW - 200)])
    assert limit_signal.recent_limit_hits(NOW - 100, NOW, root=tmp_path) == []


def test_lookback_bounds_how_far_back_a_refusal_counts(tmp_path):
    _write(tmp_path / "proj" / "s1.jsonl", [
        _refusal(NOW - limit_signal.LOOKBACK_S - 60),
    ])
    assert limit_signal.recent_limit_hits(0.0, NOW, root=tmp_path) == []


def test_transcripts_untouched_since_are_not_read(tmp_path):
    # A refusal inside the window in a file whose mtime says otherwise is
    # never opened: the mtime filter is what keeps a tick cheap.
    _write(tmp_path / "proj" / "s1.jsonl", [_refusal(NOW - 10)], mtime=NOW - 900)
    assert limit_signal.recent_limit_hits(NOW - 300, NOW, root=tmp_path) == []


def test_tool_results_are_skipped(tmp_path):
    _write(tmp_path / "proj" / "s1" / "tool-results" / "x.jsonl", [_refusal(NOW - 10)])
    assert limit_signal.recent_limit_hits(NOW - 300, NOW, root=tmp_path) == []


def test_text_only_refusal_still_counts(tmp_path):
    record = _refusal(NOW - 10)
    for key in ("apiError", "apiErrorParams"):
        record.pop(key)
    _write(tmp_path / "proj" / "s1.jsonl", [record])
    hits = limit_signal.recent_limit_hits(NOW - 300, NOW, root=tmp_path)
    assert len(hits) == 1
    assert hits[0].resets_at is None


def test_other_api_errors_do_not_count(tmp_path):
    record = _refusal(NOW - 10)
    record.pop("apiError")
    record.pop("apiErrorParams")
    record["message"]["content"][0]["text"] = (
        "API Error: Connection lost mid-response."
    )
    _write(tmp_path / "proj" / "s1.jsonl", [record])
    assert limit_signal.recent_limit_hits(NOW - 300, NOW, root=tmp_path) == []


def test_a_cut_first_line_in_the_tail_is_dropped(tmp_path, monkeypatch):
    path = _write(tmp_path / "proj" / "s1.jsonl", [
        {"type": "user", "timestamp": _iso(NOW - 60), "pad": "x" * 500},
        _refusal(NOW - 10),
    ])
    refusal_line = json.dumps(_refusal(NOW - 10)) + "\n"
    monkeypatch.setattr(limit_signal, "TAIL_BYTES", len(refusal_line) + 40)
    hits = limit_signal.recent_limit_hits(NOW - 300, NOW, root=path.parent.parent)
    assert [h.at for h in hits] == [NOW - 10]


def test_missing_root_reads_nothing(tmp_path):
    assert limit_signal.recent_limit_hits(NOW - 300, NOW, root=tmp_path / "none") == []
