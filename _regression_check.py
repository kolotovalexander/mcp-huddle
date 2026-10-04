import importlib
import os
import sys
import tempfile

sys.path.insert(0, "src")

tmp_home = tempfile.mkdtemp(prefix="huddle-regress-")
os.environ["MCP_HUDDLE_HOME"] = tmp_home

from mcp_huddle import bus
importlib.reload(bus)
from mcp_huddle import server

room_id = bus.create_room("NoJudge", "Claude", 0, "/tmp/project", "session-1")
bus.invite_agent(room_id, "Worker")
req_id = server.message_post(room_id, "Claude", "Do Z", "request", to="Worker")
result_id = server.message_post(room_id, "Worker", "Done Z", "result", to="Claude", reply_to=req_id)

messages = bus._load_messages(room_id)
assert len(messages) == 2, "plain result without judge meta must not add extra messages: " + str(messages)
assert result_id == 2

# Duplicate terminal reply must still be rejected exactly as before.
try:
    server.message_post(room_id, "Worker", "Dup", "final", to="Claude", reply_to=req_id)
    raised = False
except ValueError as e:
    raised = "already answered" in str(e)
assert raised, "existing duplicate-reply guard must be unaffected"

print("regression check: OK, message count =", len(messages))
