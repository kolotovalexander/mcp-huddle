"""Shared test isolation for the file-backed huddle bus."""

import importlib

import pytest

from mcp_huddle import bus, spawn


@pytest.fixture(autouse=True)
def isolate_huddle_storage(tmp_path, monkeypatch):
    """Keep every test's file-backed rooms outside the user's huddle home.

    ``HUDDLE_HOME`` and ``BUS_DIR`` are import-time module globals, so merely
    setting the environment variable is insufficient for tests that create a
    room without reloading ``bus`` themselves.
    """
    # A reaper from the preceding test must finish before ``bus`` is reloaded
    # onto a different storage root.  Otherwise its late lifecycle callback can
    # write into the next test's room namespace and make the suite order-racy.
    spawn._drain_background_for_tests()
    monkeypatch.setenv("MCP_HUDDLE_HOME", str(tmp_path / "huddle"))
    importlib.reload(bus)
    yield
    spawn._drain_background_for_tests()
