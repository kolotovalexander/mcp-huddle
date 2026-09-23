"""Public behavior for durable room renaming."""

import os

import pytest

from mcp_huddle import bus, server


def test_room_rename_changes_only_the_name_and_keeps_room_history():
    room_id = bus.create_room(
        "Original title", "Human", os.getpid(), "/tmp/project", "session-1",
    )
    bus.invite_agent(room_id, "Claude")
    bus.post_message(room_id, "Claude", "Existing note", "comment")
    before = bus.get_room_info(room_id)
    messages_before = bus._load_messages(room_id)

    renamed = server.room_rename(room_id, "  Читаемое название  ", "Human")

    assert renamed["name"] == "Читаемое название"
    assert renamed["id"] == room_id
    assert {key: value for key, value in renamed.items() if key != "name"} == {
        key: value for key, value in before.items() if key != "name"
    }
    assert bus.get_room_info(room_id)["name"] == "Читаемое название"
    assert bus._load_messages(room_id) == messages_before


@pytest.mark.parametrize("name", ["", " \t ", "x" * (bus.MAX_ROOM_NAME_CHARS + 1)])
def test_room_rename_rejects_invalid_name(name):
    room_id = bus.create_room("Original", "Human", os.getpid(), "", "session-1")

    with pytest.raises(ValueError):
        bus.rename_room(room_id, name, "Human")

    assert bus.get_room_info(room_id)["name"] == "Original"


def test_room_rename_requires_owner_and_existing_room():
    room_id = bus.create_room("Original", "Human", os.getpid(), "", "session-1")

    with pytest.raises(PermissionError, match="room owner"):
        bus.rename_room(room_id, "Changed", "Claude")
    with pytest.raises(ValueError, match="not found"):
        bus.rename_room("missing-room", "Changed", "Human")

    assert bus.get_room_info(room_id)["name"] == "Original"
