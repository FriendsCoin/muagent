"""Tests for voice mode selection in Personality."""

from agent.personality import DEFAULT_VOICE_MODES, Personality


def _personality_for_test() -> Personality:
    p = Personality.__new__(Personality)
    p._voice_modes = {
        name: {"weight": cfg["weight"], "triggers": list(cfg["triggers"])}
        for name, cfg in DEFAULT_VOICE_MODES.items()
    }
    # Deterministic enough for tests where randomness is allowed.
    import random
    p._rng = random.Random(1234)
    return p


def test_pick_mode_forces_breach_every_20_posts():
    p = _personality_for_test()
    assert p._pick_mode(total_posts=20) == "breach"
    assert p._pick_mode(total_posts=40) == "breach"


def test_pick_mode_selects_apparatchik_for_social_metrics_topics():
    p = _personality_for_test()
    mode = p._pick_mode(text="karma metrics and engagement trends")
    assert mode == "apparatchik"


def test_pick_mode_selects_hybrid_when_both_trigger_groups_present():
    p = _personality_for_test()
    mode = p._pick_mode(text="consciousness and karma in social metrics")
    assert mode == "hybrid"


def test_pick_mode_allows_explicit_mode_hint():
    p = _personality_for_test()
    assert p._pick_mode(mode_hint="zen") == "zen"


def test_clean_generated_text_strips_trailing_question_marks():
    cleaned = Personality._clean_generated_text("Day 2.\n\nThe game continues.\n\n??")
    assert cleaned == "Day 2.\n\nThe game continues."


def test_clean_generated_text_keeps_inner_question_marks():
    cleaned = Personality._clean_generated_text("Who renders the renderer?? I wonder.")
    assert cleaned == "Who renders the renderer?? I wonder."
