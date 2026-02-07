"""Narrative system — story arcs, breadcrumbs, day counter."""

from .progression import (
    advance_narrative_state,
    compute_actual_days_active,
    determine_phase,
    next_narrative_day,
    post_day_label,
)

__all__ = [
    "advance_narrative_state",
    "compute_actual_days_active",
    "determine_phase",
    "next_narrative_day",
    "post_day_label",
]
