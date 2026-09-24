"""Four read-only acceptance checks against a running Huddle dashboard.

Usage: python3 tools/check_dashboard_swarm.py [http://127.0.0.1:8014]
Prints aggregate results only; it never prints room messages or credentials.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import urlencode
from urllib.request import urlopen


BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8014"
RESULTS: list[tuple[str, bool, str]] = []


def get(path: str):
    with urlopen(BASE + path, timeout=12) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {path}")
        return response.read()


def record(name: str, action) -> None:
    try:
        detail = action()
        RESULTS.append((name, True, detail))
    except Exception as exc:
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))


def interface() -> str:
    html = get("/dashboard")
    css = get("/static/dashboard.css")
    js = get("/static/dashboard.js")
    assert b"room-list" in html and b"chat-area" in html
    assert len(css) > 1000 and len(js) > 1000
    return "dashboard, CSS и JS доступны"


rooms: list[dict] = []


def inventory_search() -> str:
    global rooms
    rooms = json.loads(get("/api/rooms"))
    assert isinstance(rooms, list) and rooms
    ids = [room["id"] for room in rooms]
    assert len(ids) == len(set(ids)), "дубли ID комнат"
    pilot = next(room for room in rooms if room.get("swarm_pilot"))
    query = urlencode({"q": pilot["name"]})
    found = json.loads(get("/api/rooms_search?" + query))["results"]
    assert any(item["id"] == pilot["id"] and item["title_match"] for item in found)
    return f"{len(rooms)} комнат, поиск по названию вернул нужную"


def four_modes() -> str:
    expected = {"council", "relay", "team", "swarm"}
    selected = {}
    for room in rooms:
        pilot = room.get("swarm_pilot") or {}
        mode = pilot.get("mode")
        if mode in expected and pilot.get("final") and mode not in selected:
            selected[mode] = room
    assert set(selected) == expected, f"нет завершённых режимов: {sorted(expected - set(selected))}"
    for mode, room in selected.items():
        payload = json.loads(get("/api/messages_json?" + urlencode({"room_id": room["id"]})))
        messages = payload["messages"]
        ids = [msg["id"] for msg in messages]
        assert ids == sorted(set(ids)), f"нарушен порядок сообщений: {mode}"
        kinds = {msg.get("kind") for msg in messages}
        assert {"request", "result", "final"} <= kinds, f"неполный цикл: {mode}"
        assert payload["room"]["id"] == room["id"]
    return "Совет, Эстафета, Команда и Рой: запрос → результат → итог"


def runtime_health() -> str:
    health = json.loads(get("/api/health"))
    stale = int(health.get("stale_leases", -1))
    unowned = int(health.get("unowned_leases", -1))
    affected = [
        room["room_id"]
        for room in health.get("rooms", [])
        if any(agent.get("stale_lease") or agent.get("unowned_lease")
               for agent in room.get("agents", {}).values())
    ]
    assert stale == 0 and unowned == 0, (
        f"зависших заявок={stale}, заявок без владельца={unowned}, комнаты={affected[:5]}"
    )
    return "нет зависших заявок или заявок без владельца"


record("1. Интерфейс", interface)
record("2. Комнаты и поиск", inventory_search)
record("3. Четыре режима", four_modes)
record("4. Состояние процессов", runtime_health)
for name, passed, detail in RESULTS:
    print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
print(f"Итого: {sum(passed for _, passed, _ in RESULTS)}/4")
raise SystemExit(0 if all(passed for _, passed, _ in RESULTS) else 1)
