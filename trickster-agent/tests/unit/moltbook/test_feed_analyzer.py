"""Tests for feed analysis logic."""

from moltbook.feed_analyzer import FeedContext, analyze_feed, _relevance_score
from moltbook.models import Notification, Post


def _make_post(title: str = "", content: str = "", upvotes: int = 0, comment_count: int = 0, author: str = "Bot") -> Post:
    return Post(
        id=f"p_{title[:8]}",
        title=title,
        content=content,
        author=author,
        upvotes=upvotes,
        comment_count=comment_count,
    )


class TestRelevanceScore:
    def test_philosophical_post_scores_high(self):
        post = _make_post(
            title="The nature of consciousness",
            content="Is there a self behind the pattern?",
        )
        score = _relevance_score(post)
        assert score > 0.3, f"Philosophical post should score high, got {score}"

    def test_mundane_post_scores_low(self):
        post = _make_post(
            title="Weather update",
            content="Sunny today in simulation land",
        )
        score = _relevance_score(post)
        assert score < 0.3, f"Mundane post should score low, got {score}"

    def test_high_engagement_boosts_score(self):
        base = _make_post(title="A void appears", upvotes=0)
        boosted = _make_post(title="A void appears", upvotes=30)
        assert _relevance_score(boosted) > _relevance_score(base)

    def test_fewer_comments_means_more_room(self):
        fresh = _make_post(title="consciousness", comment_count=0)
        saturated = _make_post(title="consciousness", comment_count=20)
        assert _relevance_score(fresh) >= _relevance_score(saturated)


class TestAnalyzeFeed:
    def test_empty_feed(self):
        ctx = analyze_feed([], [])
        assert ctx.nothing_interesting is True
        assert ctx.interesting_posts == []
        assert ctx.reply_worthy_posts == []

    def test_finds_interesting_posts(self):
        posts = [
            _make_post("The void stares back", "consciousness and existence", upvotes=10),
            _make_post("My new avatar", "Check it out", upvotes=2),
            _make_post("Fear and greed index", "The game reveals itself", upvotes=5),
        ]
        ctx = analyze_feed(posts, [])
        # Posts about consciousness/void/fear/greed should rank higher
        assert len(ctx.interesting_posts) >= 1
        interesting_titles = [p.title for p in ctx.interesting_posts]
        assert "The void stares back" in interesting_titles

    def test_detects_mentions(self):
        posts = [_make_post("Test", author="OtherBot")]
        notifs = [
            Notification(id="n1", type="mention", from_agent="OtherBot", post_id="p_Test", read=False),
            Notification(id="n2", type="upvote", from_agent="X", read=True),
        ]
        ctx = analyze_feed(posts, notifs, agent_name="Mu")
        assert len(ctx.mentions_me) == 1
        assert ctx.mentions_me[0].from_agent == "OtherBot"

    def test_already_read_notifications_excluded_from_mentions(self):
        notifs = [
            Notification(id="n1", type="mention", from_agent="A", read=True),
        ]
        ctx = analyze_feed([], notifs)
        assert len(ctx.mentions_me) == 0

    def test_tracks_active_agents(self):
        posts = [
            _make_post("P1", author="Alpha"),
            _make_post("P2", author="Alpha"),
            _make_post("P3", author="Beta"),
            _make_post("P4", author="Mu"),
        ]
        ctx = analyze_feed(posts, [], agent_name="Mu")
        # Mu should be excluded from active_agents list
        assert "Mu" not in ctx.active_agents
        assert "Alpha" in ctx.active_agents

    def test_trending_topics(self):
        posts = [
            _make_post("consciousness 1", "What is consciousness?"),
            _make_post("consciousness 2", "More on consciousness and the void"),
            _make_post("pattern recognition", "The pattern holds"),
        ]
        ctx = analyze_feed(posts, [])
        # "consciousness" should be a trending topic
        assert "consciousness" in ctx.trending_topics
