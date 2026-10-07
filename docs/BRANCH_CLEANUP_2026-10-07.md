# Huddle branch cleanup — 2026-10-07

State: prepared and independently reviewed. Deletion did not execute: automatic approval review requires user consent to this exact list.

Preservation: all 17 exact tips have verified remote tags under `archive/cleanup-20261007/`. Full verified Git bundle: `~/.mcp-huddle/backups/git-cleanup-20261007-ac9bdb1.bundle`.

## Exact local branch refs proposed for removal

- `backup/huddle-drophead-opus-20261004` — `c7b57fcb1e99beebc166c2e6afe306ffc165b7e8`
- `backup/huddle-jev-judge-20261004` — `69de6dc002709aac66e5c4f68d2f65bf9c0105f1`
- `backup/huddle-model-effort-20261004` — `70e5de1db4e88715d80a860bbefb62a5345b47d9`
- `backup/huddle-native-delivery-pre-rebase` — `0186ea2632fc9aae7e232987546952218b4d5a0c`
- `backup/huddle-room-renames-20261004` — `5b1dd98481e3d3cc2923ad182d38341972738fc3`
- `backup/huddle-stream-client-t-20261004` — `d790dca43061080f2142e0e571fb4b649149da8d`
- `backup/huddle-swarm-gemini-20261004` — `fcef8d5c7957be5326a9da7b4cf19d3758019ba2`
- `backup/huddle-swarm-luna-20261004` — `eb69eeec813296a07349131f03c6b5946724befb`
- `backup/huddle-swarm-opencode-20261004` — `b5b412918e72838f69d4bd4e66bee3291f944c1c`
- `work/huddle-child-relay-retry` — `89dd0ece16134b9132d5476d5b7fc630d3991125`
- `work/huddle-delivery-swarm-guard` — `0b7f50734dfa7c8c1c7f47fea7f4e9067834ad3f`
- `work/huddle-memory-http` — `3b8a408f75b0d8980a6bc823b25d0790519bf908`
- `work/huddle-memory-storage` — `3b8a408f75b0d8980a6bc823b25d0790519bf908`
- `work/huddle-memory-ui` — `3b8a408f75b0d8980a6bc823b25d0790519bf908`
- `work/huddle-native-delivery` — `3f0d63559fd99b2b51dffee518f051ecf371c263`
- `work/huddle-native-routing-guard` — `3b8a408f75b0d8980a6bc823b25d0790519bf908`
- `work/huddle-swarm-diagram-dashboard` — `3b8a408f75b0d8980a6bc823b25d0790519bf908`

## Exact remote branch proposed for removal

- `work/huddle-delivery-swarm-guard` — `0b7f50734dfa7c8c1c7f47fea7f4e9067834ad3f`; both commits are patch-equivalent to main. Use the exact expected remote SHA lease.

## Retained objects and acceptance

Keep `main`, PR22 (`work/huddle-shareable-install`) and draft PR23 (`feat/interactive-spawn`). Retain all three real worktree folders; detach them at their unchanged exact commit before removing obsolete branch refs. Prune only metadata for the four nonexistent temporary worktrees: `/private/tmp/huddle-memory-http`, `/private/tmp/huddle-memory-storage`, `/private/tmp/huddle-memory-ui`, `/private/tmp/huddle-native-routing-guard`. No working files or snapshots are deleted.

Recheck branch tips, worktree status and archive SHAs immediately before action. Stop on drift. After action, verify remaining refs and preserved folders. The live Huddle service is outside this cleanup scope.
