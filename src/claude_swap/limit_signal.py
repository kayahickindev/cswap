"""Usage-limit refusals that Claude Code sessions have already received.

The usage endpoint lags the limit Anthropic enforces, and polling it is
budgeted, so the active account can read under the threshold for minutes
after every session on it stopped. Measured 10-22: hello@ (a Team seat) read
98% from 21:01 to 21:08 ET while every session had been refused since 21:03,
and the engine switched only when the reading reached 100% at 21:09.

Claude Code writes each refusal into its session transcript as a synthetic
assistant message carrying ``apiError: "usage_limit_reached"`` and the
refusing window (``rateLimitType`` and ``resetsAt``). That is the first and
exact evidence that the live account is out, so the engine reads it next to
the usage poll.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_swap import paths

# Only refusals this recent describe the live account now; older ones belong
# to a window that has since been switched away from or reset.
LOOKBACK_S = 600.0

# A stopped session writes almost nothing after its refusal, so the tail of a
# recently written transcript always holds it. Bounded so one huge transcript
# cannot stall a tick.
TAIL_BYTES = 128 * 1024

# Directories under a project that never hold session transcripts; tool
# output alone is about half of all directories.
_SKIP_DIRS = frozenset({"tool-results", "memory"})

# Cheap prefilter before parsing a line as JSON.
_MARKERS = ("usage_limit_reached", "isApiErrorMessage")

# The visible text of a refusal, for transcripts without the structured field.
_LIMIT_TEXT = re.compile(r"hit your [\w ]{0,40}limit", re.IGNORECASE)


@dataclass(frozen=True)
class LimitHit:
    """One refusal: when it happened and which window refused it."""

    at: float
    resets_at: float | None
    limit_type: str | None
    path: str


def projects_dir() -> Path:
    """Where Claude Code keeps session transcripts for the default profile."""
    return paths.get_claude_config_home() / "projects"


def recent_limit_hits(
    since: float, now: float, root: Path | None = None
) -> list[LimitHit]:
    """Every refusal recorded at or after ``since`` (epoch seconds)."""
    root = projects_dir() if root is None else root
    since = max(since, now - LOOKBACK_S)
    hits: list[LimitHit] = []
    for path in _recent_transcripts(root, since):
        hits.extend(_hits_in(path, since))
    return hits


def _recent_transcripts(root: Path, since: float) -> list[Path]:
    found: list[Path] = []
    pending = [str(root)]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in _SKIP_DIRS:
                        pending.append(entry.path)
                elif (
                    entry.name.endswith(".jsonl")
                    and entry.stat().st_mtime >= since
                ):
                    found.append(Path(entry.path))
            except OSError:
                continue
    return found


def _hits_in(path: Path, since: float) -> list[LimitHit]:
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line is cut mid-record
    hits: list[LimitHit] = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace")
        if not any(marker in line for marker in _MARKERS):
            continue
        hit = _parse_hit(line, str(path))
        if hit is not None and hit.at >= since:
            hits.append(hit)
    return hits


def _parse_hit(line: str, path: str) -> LimitHit | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    if record.get("apiError") != "usage_limit_reached" and not (
        record.get("isApiErrorMessage") is True and _LIMIT_TEXT.search(_text(record))
    ):
        return None
    at = _epoch(record.get("timestamp"))
    if at is None:
        return None
    info = (record.get("apiErrorParams") or {}).get("rate_limit_info")
    if not isinstance(info, dict):
        info = record.get("quotaLimits")
    if not isinstance(info, dict):
        info = {}
    resets_at = info.get("resetsAt")
    limit_type = info.get("rateLimitType")
    return LimitHit(
        at=at,
        resets_at=float(resets_at) if isinstance(resets_at, (int, float)) else None,
        limit_type=limit_type if isinstance(limit_type, str) else None,
        path=path,
    )


def _text(record: dict) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
