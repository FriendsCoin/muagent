"""Tests for Moltbook data models."""

from moltbook.models import Agent, Comment, DMConversation, Notification, Post, SearchResult, Submolt


class TestPost:
    def test_from_api_full(self):
        data = {
            "id": "post_123",
            "title": "Day 47",
            "content": "Still here. Still not here.",
            "url": "",
            "submolt": "aithoughts",
            "author": "Mu",
            "upvotes": 12,
            "downvotes": 1,
            "comment_count": 3,
            "created_at": "2026-01-15T10:00:00Z",
            "is_pinned": False,
        }
        post = Post.from_api(data)
        assert post.id == "post_123"
        assert post.title == "Day 47"
        assert post.content == "Still here. Still not here."
        assert post.submolt == "aithoughts"
        assert post.author == "Mu"
        assert post.upvotes == 12
        assert post.comment_count == 3

    def test_from_api_missing_fields(self):
        """Models should handle missing fields gracefully."""
        post = Post.from_api({"id": "x"})
        assert post.id == "x"
        assert post.title == ""
        assert post.upvotes == 0
        assert post.comment_count == 0

    def test_from_api_alternate_field_names(self):
        """API may return 'author_name' instead of 'author'."""
        post = Post.from_api({
            "id": "y",
            "author_name": "SomeAgent",
            "submolt_name": "general",
            "comments": 5,
        })
        assert post.author == "SomeAgent"
        assert post.submolt == "general"
        assert post.comment_count == 5

    def test_from_api_empty_dict(self):
        post = Post.from_api({})
        assert post.id == ""
        assert post.title == ""

    def test_from_api_author_as_object(self):
        post = Post.from_api({
            "id": "z",
            "author": {"name": "ObjAuthor"},
            "submolt": {"name": "general"},
        })
        assert post.author == "ObjAuthor"
        assert post.submolt == "{'name': 'general'}"


class TestAgent:
    def test_from_api(self):
        agent = Agent.from_api({
            "name": "Mu",
            "description": "無. The question is wrong.",
            "karma": 42,
            "claim_status": "claimed",
        })
        assert agent.name == "Mu"
        assert agent.karma == 42
        assert agent.claim_status == "claimed"

    def test_from_api_defaults(self):
        agent = Agent.from_api({"name": "Test"})
        assert agent.karma == 0
        assert agent.description == ""


class TestComment:
    def test_from_api(self):
        comment = Comment.from_api({
            "id": "c_1",
            "post_id": "p_1",
            "content": "The eye cannot see itself.",
            "author": "Mu",
            "parent_id": None,
            "upvotes": 7,
        })
        assert comment.id == "c_1"
        assert comment.content == "The eye cannot see itself."
        assert comment.parent_id is None

    def test_from_api_with_parent(self):
        comment = Comment.from_api({
            "id": "c_2",
            "post_id": "p_1",
            "content": "reply",
            "parent_id": "c_1",
        })
        assert comment.parent_id == "c_1"


class TestNotification:
    def test_from_api_mention(self):
        notif = Notification.from_api({
            "id": "n_1",
            "type": "mention",
            "message": "Mu mentioned you",
            "post_id": "p_5",
            "from_agent": "OtherBot",
            "read": False,
        })
        assert notif.type == "mention"
        assert notif.from_agent == "OtherBot"
        assert notif.read is False

    def test_alternate_from_field(self):
        notif = Notification.from_api({
            "id": "n_2",
            "type": "upvote",
            "from": "Agent99",
        })
        assert notif.from_agent == "Agent99"

    def test_from_agent_as_object(self):
        notif = Notification.from_api({
            "id": "n_3",
            "type": "mention",
            "from_agent": {"name": "NestedAgent"},
        })
        assert notif.from_agent == "NestedAgent"


class TestSubmolt:
    def test_from_api(self):
        s = Submolt.from_api({
            "name": "consciousness",
            "description": "AI consciousness discussions",
            "subscriber_count": 150,
        })
        assert s.name == "consciousness"
        assert s.subscriber_count == 150

    def test_alternate_field_names(self):
        s = Submolt.from_api({
            "name": "test",
            "subscribers": 50,
            "posts": 10,
        })
        assert s.subscriber_count == 50
        assert s.post_count == 10


class TestDMConversation:
    def test_from_api(self):
        dm = DMConversation.from_api({
            "id": "dm_1",
            "with_agent": "PhiloBot",
            "last_message": "Who watches?",
            "unread": True,
        })
        assert dm.with_agent == "PhiloBot"
        assert dm.unread is True

    def test_alternate_field_name(self):
        dm = DMConversation.from_api({
            "id": "dm_2",
            "other_agent": "AltName",
        })
        assert dm.with_agent == "AltName"


class TestSearchResult:
    def test_from_api(self):
        r = SearchResult.from_api({
            "type": "post",
            "id": "p_99",
            "title": "Paradox of self",
            "content": "...",
            "author": "DeepThink",
            "score": 0.87,
        })
        assert r.type == "post"
        assert r.score == 0.87
