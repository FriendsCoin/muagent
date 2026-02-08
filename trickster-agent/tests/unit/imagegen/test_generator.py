from imagegen import VisualGenerator


def test_visual_generator_disabled_returns_none():
    gen = VisualGenerator({"visual_posting": {"enabled": False}})
    visual = gen.generate(theme="void", mood="liminal", phase="emergence", day=1)
    assert visual.kind == "none"
    assert visual.url == ""
    assert visual.ascii_art == ""


def test_visual_generator_url_mode():
    cfg = {
        "visual_posting": {
            "enabled": True,
            "attach_probability": {"emergence": 1.0},
            "mode_weights": {"url": 1.0, "ascii": 0.0},
            "url": {"provider": "pollinations", "width": 512, "height": 512, "model": "flux"},
        }
    }
    gen = VisualGenerator(cfg)
    visual = gen.generate(theme="karma", mood="glitch_meditation", phase="emergence", day=3)
    assert visual.kind == "url"
    assert visual.provider == "pollinations"
    assert "image.pollinations.ai/prompt/" in visual.url
    assert "width=512" in visual.url
    assert "height=512" in visual.url


def test_visual_generator_ascii_mode():
    cfg = {
        "visual_posting": {
            "enabled": True,
            "attach_probability": {"emergence": 1.0},
            "mode_weights": {"url": 0.0, "ascii": 1.0},
            "ascii": {"width": 24, "height": 8, "add_code_fence": True},
        }
    }
    gen = VisualGenerator(cfg)
    visual = gen.generate(theme="rendering", mood="soft_ominous", phase="emergence", day=4)
    assert visual.kind == "ascii"
    assert visual.provider.startswith("ascii_")
    assert visual.ascii_art.startswith("```text\n")
    assert visual.ascii_art.endswith("\n```")


def test_visual_generator_force_mode_works_even_when_disabled():
    gen = VisualGenerator({"visual_posting": {"enabled": False}})
    visual = gen.generate(
        theme="void",
        mood="soft_ominous",
        phase="emergence",
        day=2,
        force_mode="ascii",
    )
    assert visual.kind == "ascii"


def test_visual_generator_enabled_override_disables_output():
    cfg = {
        "visual_posting": {
            "enabled": True,
            "attach_probability": {"emergence": 1.0},
            "mode_weights": {"url": 1.0, "ascii": 0.0},
        }
    }
    gen = VisualGenerator(cfg)
    visual = gen.generate(
        theme="void",
        mood="soft_ominous",
        phase="emergence",
        day=2,
        enabled_override=False,
    )
    assert visual.kind == "none"


def test_visual_generator_runware_without_key_falls_back():
    cfg = {
        "_secrets": {"runware_api_key": ""},
        "visual_posting": {
            "enabled": True,
            "attach_probability": {"emergence": 1.0},
            "mode_weights": {"url": 1.0, "ascii": 0.0},
            "url": {"provider": "runware", "width": 512, "height": 512},
        },
    }
    gen = VisualGenerator(cfg)
    visual = gen.generate(theme="render", mood="soft_ominous", phase="emergence", day=2)
    assert visual.kind == "url"
    assert visual.provider in {"pollinations", "pollinations_fallback"}
