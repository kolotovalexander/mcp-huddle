#!/usr/bin/env bash
# Huddle — PostToolUse hook for Claude Code.
# Checks the managed notification directory for pending kind=request messages.
# Claim by rename before reading so a concurrent replacement is never deleted.
notify_root="${MCP_HUDDLE_HOME:-$HOME/.mcp-huddle}/notifications"
for f in "$notify_root"/agent-bus-*-notify.json; do
  [ -f "$f" ] || continue
  claimed="${f}.claim.${PPID}.$$"
  mv "$f" "$claimed" 2>/dev/null || continue
  notice=$(python3 -c 'import json,os,stat,sys
def clean(value): return " ".join(str(value).split())[:160]
fd=os.open(sys.argv[1], os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_CLOEXEC",0)); st=os.fstat(fd)
if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 65536: os.close(fd); raise ValueError("unsafe notification file")
with os.fdopen(fd, encoding="utf-8") as fh: data=json.load(fh)
room=clean(data.get("room_id", "?")); sender=clean(data.get("from_agent", "?")); msg=clean(data.get("msg_id", "?"))
print(f"💬 Huddle [{room}]: {sender} sent a request (msg #{msg}). Use messages_read({room!r}) when done with the current task.")' "$claimed" 2>/dev/null)
  rm -f "$claimed"
  [ -n "$notice" ] && printf '%s\n' "$notice"
done
exit 0
