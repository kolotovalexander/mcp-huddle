"""Watchdog wiring: `_background_watchdog()` must run in BOTH stdio and HTTP
transports, exactly once per process, regardless of how many times the
underlying lifespan context manager is entered (HTTP fires it once per
session — see `_watchdog_lifespan`'s docstring in server.py).
"""
import asyncio

from mcp_huddle import server


def _fake_watchdog_factory(calls: list):
    """Build a stand-in for `_background_watchdog()` that records a start and
    then blocks until cancelled, so tests can assert liveness without the
    real 30s (bus.ZOMBIE_CHECK_SECS) sleep loop."""

    async def fake_watchdog():
        calls.append(1)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    return fake_watchdog


def test_watchdog_lifespan_starts_and_cleans_up_on_exit(monkeypatch):
    calls: list = []
    monkeypatch.setattr(server, "_background_watchdog", _fake_watchdog_factory(calls))
    assert server._watchdog_task is None

    holder: dict = {}

    async def run():
        async with server._watchdog_lifespan():
            task = server._watchdog_task
            holder["task"] = task
            assert task is not None
            # Let the fake watchdog actually start running.
            await asyncio.sleep(0)
            assert not task.done()

    asyncio.run(run())
    assert server._watchdog_task is None
    assert holder["task"].cancelled() or holder["task"].done()
    assert calls == [1]


def test_watchdog_lifespan_reentrant_does_not_double_start(monkeypatch):
    """A nested/re-entrant call (simulating an HTTP session's per-connection
    lifespan firing while the app-level lifespan already owns the watchdog)
    must NOT create a second task, and must NOT cancel the task early."""
    calls: list = []
    monkeypatch.setattr(server, "_background_watchdog", _fake_watchdog_factory(calls))

    async def run():
        async with server._watchdog_lifespan():
            outer_task = server._watchdog_task
            await asyncio.sleep(0)  # let the fake watchdog actually start
            async with server._watchdog_lifespan():
                inner_task = server._watchdog_task
                assert inner_task is outer_task
            # Inner exit was not the owner — task must still be alive.
            assert server._watchdog_task is outer_task
            assert not outer_task.done()
        assert server._watchdog_task is None
        assert outer_task.cancelled() or outer_task.done()

    asyncio.run(run())
    assert calls == [1]


def test_watchdog_lifespan_survives_early_exit_of_the_starting_entry(monkeypatch):
    """The refcount, not "whoever started it", decides when to cancel: if the
    FIRST entry to open (e.g. a per-session lifespan that happened to race
    ahead of the app-level one) exits while a LATER entry (e.g.
    combined_lifespan, covering the whole app's lifetime) is still open, the
    watchdog must keep running until that later entry also exits. An
    ownership model ("only the starter cancels") would kill the watchdog
    early here and silently starve every other still-open session."""
    calls: list = []
    monkeypatch.setattr(server, "_background_watchdog", _fake_watchdog_factory(calls))

    cm_first = server._watchdog_lifespan()
    cm_second = server._watchdog_lifespan()

    async def run():
        await cm_first.__aenter__()
        task = server._watchdog_task
        await asyncio.sleep(0)
        await cm_second.__aenter__()

        # The entry that STARTED the task exits first.
        await cm_first.__aexit__(None, None, None)
        assert server._watchdog_task is task
        assert not task.done()

        # Only once the last remaining entry exits does it get cancelled.
        await cm_second.__aexit__(None, None, None)
        assert server._watchdog_task is None
        assert task.cancelled() or task.done()

    asyncio.run(run())
    assert calls == [1]


def test_watchdog_lifespan_many_concurrent_sessions_start_exactly_one_task(monkeypatch):
    """Simulates HTTP mode: `Server.run()` (and therefore the FastMCP-level
    `_watchdog_lifespan`) fires once per session. Many concurrent 'sessions'
    entering/exiting must still only ever produce ONE live watchdog task —
    the double-start guard required by the task."""
    calls: list = []
    monkeypatch.setattr(server, "_background_watchdog", _fake_watchdog_factory(calls))

    async def fake_session(delay: float):
        async with server._watchdog_lifespan():
            await asyncio.sleep(delay)

    async def run():
        await asyncio.gather(*(fake_session(0.01 * i) for i in range(10)))

    asyncio.run(run())
    assert calls == [1]
    assert server._watchdog_task is None


def test_build_app_wires_single_watchdog_via_combined_lifespan(monkeypatch):
    """HTTP mode (build_app's combined_lifespan) must still start exactly one
    watchdog for the whole app lifetime, not per session — verified by
    driving the Starlette app's lifespan_context directly.

    `mcp.streamable_http_app()` lazily caches a `StreamableHTTPSessionManager`
    on the module-global `mcp` singleton, and that manager's `.run()` may only
    be entered once per instance (other tests, e.g. test_smoke.py, may already
    have started/stopped it in this same process). Reset the cache so this
    test gets its own fresh, not-yet-started manager instead of colliding
    with another test's spent one.
    """
    calls: list = []
    monkeypatch.setattr(server, "_background_watchdog", _fake_watchdog_factory(calls))
    monkeypatch.setattr(server.mcp, "_session_manager", None)

    app = server.build_app()

    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert server._watchdog_task is not None
            assert not server._watchdog_task.done()

    asyncio.run(run())
    assert server._watchdog_task is None
    assert calls == [1]
