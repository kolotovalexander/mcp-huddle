#!/usr/bin/env bash
# Agent Bus — Stop hook for Claude Code.
# Closes all rooms opened in this session when Claude Code exits.
SESSION_ID=$(cat /tmp/claude-session-id 2>/dev/null || echo "")
[ -z "$SESSION_ID" ] && exit 0
curl -s -X POST http://127.0.0.1:8014/mcp \
  -H "Content-Type: application/json" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"room_close_session\",\"arguments\":{\"session_id\":\"$SESSION_ID\"}}}" \
  > /dev/null 2>&1 || true
