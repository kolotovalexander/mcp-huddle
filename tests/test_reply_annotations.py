"""A conversation can address a specific message without changing result rules."""

import pytest

from mcp_huddle import bus


def test_comments_can_reply_to_prior_messages_without_changing_result_rules(tmp_path, monkeypatch):
    home = tmp_path / "huddle"
    monkeypatch.setattr(bus, "HUDDLE_HOME", home)
    monkeypatch.setattr(bus, "BUS_DIR", home / "rooms")
    monkeypatch.setattr(bus, "NOTIFICATIONS_DIR", home / "notifications")
    monkeypatch.setattr(bus, "NOTIFICATION_LOCKS_DIR", home / "internal" / "notification-locks")
    room = bus.create_room("annotations", "Human", 0)
    request = bus.post_message(room, "Human", "Question", "request", to="Claude")
    answer = bus.post_message(room, "Claude", "Answer", "result", reply_to=request)
    comment = bus.post_message(room, "Human", "About this answer", "comment", reply_to=answer)
    annotation = bus.post_message(room, "Codex", "Adding detail", "comment", reply_to=comment)

    assert [m.get("reply_to") for m in bus._load_messages(room)] == [None, request, answer, comment]
    assert annotation == 4
    with pytest.raises(ValueError, match="target must be a request"):
        bus.post_message(room, "Claude", "Formal result", "result", reply_to=comment)
    with pytest.raises(ValueError, match="target must be a request"):
        bus.post_message(room, "Human", "Follow-up request", "request", to="Claude", reply_to=comment)
