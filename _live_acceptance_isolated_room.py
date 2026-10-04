"""Isolated-room acceptance check for the Jev result-judge integration.

Safety: auto_spawn=False and no agent in DEFAULT_REGISTRY is named "Worker"
or "Coordinator", so agent_meta stays empty for this room and
_wake_agents_for_request has nothing real to wake, even for the follow-up
kind=request jev-judge posts. No real Claude/Codex/Antigravity process is
touched. Uses the REAL jev_judge/server code against a temp MCP_HUDDLE_HOME
(source-level acceptance) — this does not reach the actually-serving
127.0.0.1:45111 process, which this sandboxed session cannot inspect or
restart (see receipt "blockers").
"""
import os
import sys
import tempfile

sys.path.insert(0, "src")
os.environ["MCP_HUDDLE_HOME"] = tempfile.mkdtemp(prefix="huddle-live-accept-")

from mcp_huddle import bus, server

room_id = server.room_create(
    "JevLiveAcceptance", "Coordinator", 0,
    cwd="/tmp/project", session_id="accept-1", auto_spawn=False,
)
bus.invite_agent(room_id, "Worker")

req_id = server.message_post(room_id, "Coordinator", "Reverse a string", "request", to="Worker")
server.message_post(
    room_id, "Worker", "def reverse(s): return s[::-1]", "result", to="Coordinator", reply_to=req_id,
    meta={"judge": {
        "task": "Write a function that reverses a string",
        "acceptance_criteria": "'abc' -> 'cba', has a passing unit test",
        "result": "def reverse(s): return s[::-1]",
        "evidence": "pytest tests/test_reverse.py -q -> 1 passed",
        "version": "accept-1",
    }},
)
server._drain_judge_threads_for_tests()

print("room_id:", room_id)
for m in bus._load_messages(room_id):
    print(m.get("id"), m.get("agent"), m.get("kind"), "to=" + str(m.get("to")), (m.get("body") or "")[:140])

info = bus.get_room_info(room_id)
print("agent_meta (should be empty -> nothing real was woken):", info.get("agent_meta"))
print("jev_judge claim state:", info.get("jev_judge"))
