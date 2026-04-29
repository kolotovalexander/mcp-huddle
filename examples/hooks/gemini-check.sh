#!/usr/bin/env bash
# Agent Bus — on_tool_response hook for Gemini CLI.
# Same logic as claude-check.sh — fires after each tool response.
for f in /tmp/agent-bus-*-notify.json; do
  [ -f "$f" ] || continue
  room=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('room_id','?'))" 2>/dev/null)
  sender=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('from_agent','?'))" 2>/dev/null)
  msg_id=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('msg_id','?'))" 2>/dev/null)
  rm -f "$f"
  echo "💬 Agent Bus [${room}]: ${sender} sent a request (msg #${msg_id}). Use messages_read('${room}') when ready."
done
