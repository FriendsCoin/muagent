"""Narrative progression helpers for day/phase updates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.memory import AgentState


_PHASE_ORDER = ("emergence", "patterns", "tension", "mirror")


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _phase_max_days(phases_cfg: dict[str, Any], phase: str) -> int | None:
    phase_cfg = phases_cfg.get(phase, {})
    max_days = phase_cfg.get("duration_days_max")
    min_days = phase_cfg.get("duration_days_min")
    val = max_days if max_days is not None else min_days
    if val is None:
        return None
    return int(val)


def compute_actual_days_active(start_date: str, now: datetime | None = None) -> int:
    """Return full UTC calendar days between start and now."""
    start_dt = _parse_iso(start_date)
    if start_dt is None:
        return 0
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)
    delta = (now_dt.date() - start_dt.date()).days
    return max(0, delta)


def determine_phase(actual_days_active: int, phases_cfg: dict[str, Any]) -> str:
    """Determine phase from cumulative max durations."""
    if actual_days_active < 0:
        return "emergence"

    offset = 0
    for phase in _PHASE_ORDER[:-1]:
        max_days = _phase_max_days(phases_cfg, phase)
        if max_days is None:
            return phase
        if actual_days_active < offset + max_days:
            return phase
        offset += max_days
    return "mirror"


def next_narrative_day(current_day: int, forbidden_days: list[int]) -> int:
    """Advance day counter and skip forbidden day numbers."""
    forbidden = set(forbidden_days)
    next_day = current_day + 1
    while next_day in forbidden:
        next_day += 1
    return next_day


def post_day_label(current_day: int, posts_today: int) -> str:
    """Human-facing day label for post titles.

    First post of the day keeps classic form: "Day X".
    Subsequent posts become: "Day X - Post N".
    """
    if posts_today <= 0:
        return f"Day {current_day}"
    return f"Day {current_day} - Post {posts_today + 1}"


def advance_narrative_state(
    state: AgentState,
    cfg: dict[str, Any],
    now: datetime | None = None,
) -> None:
    """Mutate state with calendar-based day and phase progression."""
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)

    if not state.start_date:
        state.start_date = now_dt.isoformat()
    if not state.phase_start_date:
        state.phase_start_date = now_dt.isoformat()

    narrative_cfg = cfg.get("narrative", {})
    phases_cfg = narrative_cfg.get("phases", {})
    forbidden_days = narrative_cfg.get("forbidden_days", [13, 33, 66])

    state.actual_days_active = compute_actual_days_active(state.start_date, now=now_dt)

    last_dt = _parse_iso(state.last_heartbeat) or _parse_iso(state.start_date)
    if last_dt:
        elapsed_days = max(0, (now_dt.date() - last_dt.date()).days)
        for _ in range(elapsed_days):
            state.current_day = next_narrative_day(state.current_day, forbidden_days)
            # New narrative day starts with fresh daily action counters.
            state.posts_today = 0
            state.comments_today = 0

    new_phase = determine_phase(state.actual_days_active, phases_cfg)
    if new_phase != state.current_phase:
        state.current_phase = new_phase
        state.phase_start_date = now_dt.isoformat()
