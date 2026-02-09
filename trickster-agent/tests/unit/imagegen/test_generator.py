import httpx

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


def test_visual_generator_pollinations_enter_without_key_falls_back_to_public_url():
    cfg = {
        "_secrets": {"pollinations_api_key": ""},
        "visual_posting": {
            "enabled": True,
            "attach_probability": {"emergence": 1.0},
            "mode_weights": {"url": 1.0, "ascii": 0.0},
            "url": {"provider": "pollinations_enter", "width": 512, "height": 512, "model": "flux"},
        },
    }
    gen = VisualGenerator(cfg)
    visual = gen.generate(theme="render", mood="soft_ominous", phase="emergence", day=2)
    assert visual.kind == "url"
    assert visual.provider in {"pollinations", "pollinations_fallback"}
    assert visual.meta.get("pollinations_enter_fallback_used") is True
    assert visual.meta.get("pollinations_enter_error_reason") == "no_key"


def test_visual_generator_audio_mode():
    cfg = {
        "_secrets": {"pollinations_api_key": "abc123"},
        "visual_posting": {
            "enabled": True,
            "attach_probability": {"emergence": 1.0},
            "mode_weights": {"url": 0.0, "ascii": 0.0, "audio": 1.0, "video": 0.0},
            "media": {"pollinations_endpoint": "https://gen.pollinations.ai", "audio_voice": "nova"},
        },
    }
    gen = VisualGenerator(cfg)
    visual = gen.generate(theme="voice", mood="soft_ominous", phase="emergence", day=2)
    assert visual.kind == "url"
    assert visual.provider == "pollinations_audio"
    assert "/audio/" in visual.url
    assert "voice=nova" in visual.url
    assert "key=abc123" not in visual.url
    assert "key=abc123" in str((visual.meta or {}).get("signed_url", ""))


def test_visual_generator_video_force_mode_works_even_when_disabled():
    gen = VisualGenerator({"visual_posting": {"enabled": False}})
    visual = gen.generate(
        theme="void",
        mood="soft_ominous",
        phase="emergence",
        day=2,
        force_mode="video",
    )
    assert visual.kind == "url"
    assert visual.provider == "pollinations_video"
    assert "/video/" in visual.url


def test_visual_generator_video_fal_without_key_falls_back_to_pollinations_url():
    cfg = {
        "_secrets": {"fal_key": ""},
        "visual_posting": {
            "enabled": True,
            "attach_probability": {"emergence": 1.0},
            "mode_weights": {"url": 0.0, "ascii": 0.0, "audio": 0.0, "video": 1.0},
            "media": {"video_provider": "fal"},
        },
    }
    gen = VisualGenerator(cfg)
    visual = gen.generate(theme="void", mood="soft_ominous", phase="emergence", day=1)
    assert visual.kind == "url"
    # We still return a URL (pollinations video) and the upper pipeline can fall back further.
    assert visual.provider == "pollinations_video"
    assert visual.meta.get("video_provider_requested") == "fal"
    assert visual.meta.get("fal_fallback_used") is True
    assert visual.meta.get("fal_error_reason") == "no_key"


def test_visual_generator_video_fal_success(monkeypatch):
    class _Resp:
        def __init__(self, status_code: int, body: dict):
            self.status_code = status_code
            self._body = body
            self.text = str(body)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=self)

        def json(self):
            return self._body

    calls = {"post": 0, "get": 0}

    def _fake_post(url, json=None, headers=None, timeout=None):
        calls["post"] += 1
        assert "queue.fal.run" in url
        assert isinstance(json, dict) and "prompt" in json
        assert (headers or {}).get("Authorization", "").startswith("Key ")
        return _Resp(200, {"request_id": "req_123"})

    def _fake_get(url, headers=None, timeout=None, follow_redirects=None):
        calls["get"] += 1
        if url.endswith("/status"):
            return _Resp(200, {"status": "COMPLETED"})
        return _Resp(200, {"video": {"url": "https://cdn.example/video.mp4"}})

    monkeypatch.setattr("imagegen.generator.httpx.post", _fake_post)
    monkeypatch.setattr("imagegen.generator.httpx.get", _fake_get)

    cfg = {
        "_secrets": {"fal_key": "k_test"},
        "visual_posting": {
            "enabled": True,
            "attach_probability": {"emergence": 1.0},
            "mode_weights": {"url": 0.0, "ascii": 0.0, "audio": 0.0, "video": 1.0},
            "media": {"video_provider": "fal", "fal": {"timeout_seconds": 5, "poll_interval_seconds": 0.01}},
        },
    }
    gen = VisualGenerator(cfg)
    visual = gen.generate(theme="void", mood="soft_ominous", phase="emergence", day=1)
    assert visual.kind == "url"
    assert visual.provider == "fal_video"
    assert visual.url == "https://cdn.example/video.mp4"
    assert (visual.meta or {}).get("fal_request_id") == "req_123"
    assert calls["post"] == 1
    assert calls["get"] >= 2
