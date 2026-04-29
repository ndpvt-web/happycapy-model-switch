---
name: happycapy-model-switch
description: >
  Fixes model switching bugs in HappyCapy when users switch between Claude and
  MiniMax (or other non-Claude models) mid-conversation. Use this skill whenever
  the user mentions: model switching errors, "Invalid signature in thinking block",
  context loss when switching models, MiniMax and Claude switching not working,
  or wants to patch/fix HappyCapy's model switching behavior. Also invoke this
  skill proactively when users say things like "switching models breaks my chat",
  "MiniMax loses context", or "Claude gives an error after I was using MiniMax".
---

# happycapy-model-switch

Applies server-side patches to fix two bugs in HappyCapy's model switching:

1. **"Invalid signature in thinking block" errors** — when switching from MiniMax to Claude, MiniMax's thinking blocks have short hex signatures that Claude's SDK rejects with a 400 error.
2. **Context loss** — switching models causes the new model to lose all prior conversation context.

The fix is fully automated via `scripts/patch.py`. All you need to do is run it.

## Skill directory

The scripts are bundled at the skill's base directory. Find it:

```
SKILL_DIR=$(python3 -c "
import os, glob
hits = glob.glob(os.path.expanduser('~/.claude/skills/happycapy-model-switch/scripts/patch.py'))
print(os.path.dirname(hits[0]) if hits else '')
")
```

## How to apply the fix

### Standard usage (apply patches + restart server)

```bash
python3 "$SKILL_DIR/patch.py"
```

This will:
- Back up the server file (`/app/server/dist/index.js.minimax-fix-bak`)
- Apply 5 patches to the live server JS
- Restart the server via `supervisorctl`

### Check current status (no changes)

```bash
python3 "$SKILL_DIR/patch.py" --status
```

### Dry run (preview what would change)

```bash
python3 "$SKILL_DIR/patch.py" --dry-run
```

### Fix a specific stuck session

If a user is already stuck mid-conversation with a bad session:

```bash
python3 "$SKILL_DIR/patch.py" --session SESSION_UUID
```

Replace `SESSION_UUID` with the actual session ID (a UUID like `983d37aa-f6f8-...`).

### Make the fix permanent (survive redeploys)

```bash
python3 "$SKILL_DIR/patch.py" --install-watcher
```

This installs a `supervisord` event listener that automatically re-applies the patches whenever the server restarts or is redeployed.

To remove it:

```bash
python3 "$SKILL_DIR/patch.py" --uninstall-watcher
```

## What the patches do

| Patch | What it changes |
|-------|----------------|
| 1 | Adds `sessionContextSeeds` Map to the agent constructor for bridging context |
| 2 | Makes `clearSession()` also null the persisted `sdkSessionId` in the database |
| 3 | When a model switch is detected: if switching TO Claude, clears SDK session + schedules a Haiku summary; if switching to non-Claude (MiniMax), preserves the server message store so context flows naturally |
| 4 | In `startSessionQuery()`, injects the Haiku-generated summary into Claude's system prompt on the next query after a switch |
| 5 | Adds `_scheduleCompact()` (reads the conversation JSONL, summarizes via AI Gateway with `anthropic/claude-haiku-4.5`) and `_buildJSONLPath()` helper methods |

## Requirements

- HappyCapy server running at `/app/server/dist/index.js`
- `AI_GATEWAY_API_KEY` set in the environment (used by Haiku summarization)
- `sudo` access for server restart via `supervisorctl`
- Python 3.6+

## Result after applying

| Switch Direction | Behavior | Context? |
|----------------|---------|---------|
| MiniMax → Claude | Clear session + Haiku summary injected | Yes (bridged) |
| Claude → MiniMax | Preserve server message store | Yes (natural) |
| Either direction | No more "Invalid signature" errors | — |
