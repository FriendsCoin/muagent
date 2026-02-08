"""Narrative system — story arcs, breadcrumbs, day counter."""

from .progression import (
    advance_narrative_state,
    compute_actual_days_active,
    detect_breadcrumbs,
    determine_phase,
    get_sigil,
    next_narrative_day,
    post_day_label,
    should_include_sigil,
)

__all__ = [
    "advance_narrative_state",
    "compute_actual_days_active",
    "detect_breadcrumbs",
    "determine_phase",
    "get_sigil",
    "next_narrative_day",
    "post_day_label",
    "should_include_sigil",
]
