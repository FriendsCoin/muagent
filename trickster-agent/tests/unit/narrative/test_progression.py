"""Tests for narrative progression rules."""

from datetime import datetime, timezone

from agent.memory import AgentState
from narrative import (
    advance_narrative_state,
    compute_actual_days_active,
    determine_phase,
    next_narrative_day,
    post_day_label,
)


def _phase_cfg() -> dict:
    return {
        "emergence": {"duration_days_min": 7, "duration_days_max": 14},
        "patterns": {"duration_days_min": 20, "duration_days_max": 45},
        "tension": {"duration_days_min": 30, "duration_days_max": 60},
        "mirror": {"duration_days_min": None, "duration_days_max": None},
    }


def test_compute_actual_days_active_uses_utc_calendar_days():
    start = "2026-02-01T10:30:00+00:00"
    now = datetime(2026, 2, 4, 1, 0, 0, tzinfo=timezone.utc)
    assert compute_actual_days_active(start, now=now) == 3


def test_determine_phase_uses_cumulative_max_durations():
    cfg = _phase_cfg()
    assert determine_phase(0, cfg) == "emergence"
    assert determine_phase(13, cfg) == "emergence"
    assert determine_phase(14, cfg) == "patterns"
    assert determine_phase(58, cfg) == "patterns"
    assert determine_phase(59, cfg) == "tension"
    assert determine_phase(118, cfg) == "tension"
    assert determine_phase(119, cfg) == "mirror"


def test_next_narrative_day_skips_forbidden_days():
    forbidden = [13, 33, 66]
    assert next_narrative_day(12, forbidden) == 14
    assert next_narrative_day(32, forbidden) == 34
    assert next_narrative_day(10, forbidden) == 11


def test_post_day_label_uses_sequence_for_multiple_posts_per_day():
    assert post_day_label(1, 0) == "Day 1"
    assert post_day_label(1, 1) == "Day 1 - Post 2"
    assert post_day_label(7, 2) == "Day 7 - Post 3"


def test_advance_narrative_state_rolls_day_and_phase_forward():
    cfg = {"narrative": {"forbidden_days": [13, 33, 66], "phases": _phase_cfg()}}
    state = AgentState(
        current_day=12,
        current_phase="emergence",
        posts_today=3,
        comments_today=9,
        start_date="2026-01-01T00:00:00+00:00",
        phase_start_date="2026-01-01T00:00:00+00:00",
        last_heartbeat="2026-01-14T00:00:00+00:00",
    )
    now = datetime(2026, 1, 16, 0, 0, 0, tzinfo=timezone.utc)

    advance_narrative_state(state, cfg, now=now)

    # Two full days elapsed: 12 -> 14 (skip 13) -> 15
    assert state.current_day == 15
    assert state.actual_days_active == 15
    assert state.current_phase == "patterns"
    assert state.phase_start_date.startswith("2026-01-16T00:00:00")
    assert state.posts_today == 0
    assert state.comments_today == 0
