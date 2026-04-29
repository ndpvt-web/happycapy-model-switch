# happycapy-model-switch

Fixes model switching bugs in HappyCapy when users switch between Claude and MiniMax (or other non-Claude models) mid-conversation.

## Problems Solved

1. **"Invalid signature in thinking block" errors** -- When switching from MiniMax to Claude, MiniMax's thinking blocks carry short hex signatures that Claude's SDK rejects.

2. **Context loss when switching models** -- Without this fix, switching models mid-conversation causes the new model to lose all prior context.

## How It Works

| Switch Direction | What Happens | Context Preserved? |
|-----------------|-------------|-------------------|
| **MiniMax to Claude** | Clears SDK session + generates a compact summary via Haiku + injects into Claude's system prompt | Yes (bridged summary) |
| **Claude to MiniMax** | Preserves the server's message store (no clearing) | Yes (natural -- MiniMax reads existing messages) |

### 5 Server Patches

1. **sessionContextSeeds Map** -- Adds storage for context bridge summaries in the agent constructor
2. **clearSession DB cleanup** -- Nulls the persisted `sdkSessionId` in the database when a session is cleared
3. **Conditional model switch** -- Only clears session when target is Claude; preserves server message store when target is non-Claude
4. **Context injection** -- Injects the Haiku-generated summary into Claude's system prompt on the next query
5. **Helper methods** -- `_scheduleCompact` (reads JSONL, summarizes via AI Gateway) and `_buildJSONLPath`

### Context Summarization

When switching TO Claude, the patcher reads the conversation from the old JSONL session file and sends it to `anthropic/claude-haiku-4.5` via the HappyCapy AI Gateway for a concise summary. This summary is then injected into Claude's system prompt so it can continue the conversation with full awareness of prior context.

## Usage

```bash
# Apply patches and restart server
python3 scripts/patch.py

# Check current patch status
python3 scripts/patch.py --status

# Dry run (see what would change)
python3 scripts/patch.py --dry-run

# Repair a stuck session (strip bad thinking blocks from JSONL)
python3 scripts/patch.py --session SESSION_ID

# Install auto-reapply watcher (survives redeploys)
python3 scripts/patch.py --install-watcher

# Uninstall watcher
python3 scripts/patch.py --uninstall-watcher
```

## Requirements

- HappyCapy server running at `/app/server/dist/index.js`
- `AI_GATEWAY_API_KEY` environment variable (for Haiku summarization)
- `supervisord` for server restart and optional watcher

## Files

```
scripts/
  patch.py      # Main patcher -- applies 5 patches to the server JS
  watcher.py    # Supervisord event listener -- auto-reapplies patches after redeploy
```

## License

MIT
