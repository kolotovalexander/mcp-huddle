"""Shared test isolation for the file-backed huddle bus."""

import importlib

import pytest

from mcp_huddle import bus


@pytest.fixture(autouse=True)
def isolate_huddle_storage(tmp_path, monkeypatch):
    """Keep every test's file-backed rooms outside the user's huddle home.

    ``HUDDLE_HOME`` and ``BUS_DIR`` are import-time module globals, so merely
    setting the environment variable is insufficient for tests that create a
    room without reloading ``bus`` themselves.
    """
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    importlib.reload(bus)
    yield
