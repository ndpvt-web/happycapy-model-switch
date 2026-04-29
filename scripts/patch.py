#!/usr/bin/env python3
"""
happycapy-model-switch patcher
-------------------------------
Fixes model switching bugs in HappyCapy when users switch between
Claude and MiniMax (or other non-Claude models) mid-conversation.

Solves two problems:
  1. "Invalid signature in thinking block" errors (MiniMax -> Claude)
  2. Context loss when switching models in either direction

How it works:
  - When switching TO Claude: clears the SDK session (prevents signature
    errors) and bridges context via a Haiku-generated summary injected
    into Claude's system prompt.
  - When switching FROM Claude to non-Claude: preserves the server's
    message store so the target model naturally retains full context.
  - Summarization uses the AI Gateway with anthropic/claude-haiku-4.5.

Usage:
  python3 patch.py                          # apply server patches + restart
  python3 patch.py --session SESSION_ID    # also fix a specific session
  python3 patch.py --dry-run               # check status without changing anything
  python3 patch.py --session SESSION_ID --session-only  # only fix the session
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
SERVER_JS = "/app/server/dist/index.js"
SESSIONS_BASE = Path.home() / "data" / "sessions"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
WORKSPACE_BASE = Path.home() / "a0" / "workspace"
# ─────────────────────────────────────────────────────────────────────────────


def bold(s): return f"\033[1m{s}\033[0m"
def green(s): return f"\033[32m{s}\033[0m"
def yellow(s): return f"\033[33m{s}\033[0m"
def red(s): return f"\033[31m{s}\033[0m"
def ok(s): print(green(f"  \u2713 {s}"))
def skip(s): print(yellow(f"  ~ {s}"))
def fail(s): print(red(f"  \u2717 {s}"))
def info(s): print(f"  {s}")


# ─────────────────────────────────────────────────────────────────────────────
# Patch definitions
# Each patch is (name, detection_string, old_string, new_string)
# detection_string is a short unique snippet that only exists AFTER patching.
# ─────────────────────────────────────────────────────────────────────────────

SUPERVISORD_CONF = "/etc/supervisord.conf"
WATCHER_SECTION = "eventlistener:minimax-fix-watcher"
WATCHER_SCRIPT = str(Path(__file__).parent / "watcher.py")
WATCHER_CONF_BLOCK = f"""
[{WATCHER_SECTION}]
command=python3 {WATCHER_SCRIPT}
events=PROCESS_STATE_RUNNING
autostart=true
autorestart=true
stdout_logfile=/dev/null
stderr_logfile=/var/log/minimax-fix-watcher.log
"""

# ─── Patch 1: Add sessionContextSeeds Map for bridging context ───────────────
PATCH_1 = (
    "sessionContextSeeds Map in Vx constructor",
    "sessionContextSeeds=new Map",
    "this.sessionTimeoutOverrides=new Map;this.subagentFileWatchers",
    "this.sessionContextSeeds=new Map;this.sessionTimeoutOverrides=new Map;this.subagentFileWatchers",
)

# ─── Patch 2: clearSession also nulls sdkSessionId in DB ─────────────────────
PATCH_2 = (
    "SessionManager.clearSession() nulls persisted sdkSessionId",
    "Failed to clear persisted sdkSessionId",
    "this.sdkSessionIds.delete(e),console.log(`[SessionManager] Session ${e} cleared`)",
    (
        "this.sdkSessionIds.delete(e),"
        "Te.updateSdkSessionId(e,null).catch(r=>console.error(`[SessionManager] Failed to clear persisted sdkSessionId for session ${e}:`,r)),"
        "console.log(`[SessionManager] Session ${e} cleared`)"
    ),
)

# ─── Patch 3: Model switch — clear session + schedule compact ────────────────
# Conditional model switch handling:
#   - TO Claude: clear session + null sdkSessionId + schedule compact (bridge context via Haiku)
#   - TO MiniMax (or other non-Claude): do NOT clear session — MiniMax reads from the
#     server's message store, so preserving it gives MiniMax full context naturally.
PATCH_3 = (
    "sendMessage() conditional model switch handling",
    "s.model.toLowerCase().includes(\"claude\")",
    (
        "console.log(`[Agent] Model provider changed: ${f.model} -> ${s.model}, clearing session ${n}`),"
        "this.clearSession(n))"
    ),
    (
        "console.log(`[Agent] Model provider changed: ${f.model} -> ${s.model}, session ${n}`),"
        "((_or)=>{"
        "if(s.model.toLowerCase().includes(\"claude\")){"
        "console.log(`[Agent] Target is Claude - clearing SDK session and bridging context`);"
        "this.clearSession(n);r=null;this._scheduleCompact(n,_or,s?.cwd,f.model,s.model)"
        "}else{"
        "console.log(`[Agent] Target is non-Claude (${s.model}) - preserving server message store for context`)"
        "}"
        "})(r))"
    ),
)

# ─── Patch 4: startSessionQuery injects context seed (for Claude targets) ────
PATCH_4 = (
    "startSessionQuery() injects compact context seed",
    "Injected compact context seed for session",
    "buildQueryOptions({sessionWorkspace:r,sdkSessionId:s,canUseToolCallback:a,queryConfig:o});A.hooks=",
    (
        "buildQueryOptions({sessionWorkspace:r,sdkSessionId:s,canUseToolCallback:a,queryConfig:o});"
        "{const _sp=this.sessionContextSeeds.get(e);"
        "if(_sp){"
        "this.sessionContextSeeds.delete(e);"
        "const _sv=await _sp;"
        "if(_sv){"
        "if(!A.systemPrompt)A.systemPrompt={type:\"preset\",preset:\"claude_code\"};"
        "A.systemPrompt.append=(A.systemPrompt.append||\"\")+`\\n\\n${_sv}`;"
        "console.log(`[Agent] Injected compact context seed for session ${e} (${_sv.length} chars)`)}}}"
        "A.hooks="
    ),
)

# ─── Patch 5: Insert helper methods into Vx class ────────────────────────────
# Uses AI Gateway (https://ai-gateway.happycapy.ai) with correct model name
# (anthropic/claude-haiku-4.5) and OpenAI-compatible format.
PATCH_5_DETECTION = "ai-gateway.happycapy.ai/api/v1/chat/completions"
PATCH_5_OLD = "console.log(`[Agent] Session ${e} cleared`)}detectModelProviderChange"
PATCH_5_NEW = (
    "console.log(`[Agent] Session ${e} cleared`)}"
    # ── _scheduleCompact ─────────────────────────────────────────────────────
    "_scheduleCompact(e,i,n,r,s){"
    "if(!i){console.log(\"[Agent] Compact skipped: no old sdkSessionId\");return}"
    "console.log(`[Agent] Scheduling compact for session ${e}, switch: ${r}->${s}, old sdk: ${i}`);"
    "const o=(async()=>{"
    "try{"
    "const t=await this.sessionManager.getSessionWorkspace(e,n),"
    "c=this._buildJSONLPath(t,i);"
    "if(!c)return null;"
    "const l=require(\"fs\"),u=require(\"path\");"
    "if(!l.existsSync(c)){console.log(`[Agent] Compact: JSONL not found at ${c}`);return null}"
    "const p=l.readFileSync(c,\"utf8\").split(\"\\n\").filter(Boolean);"
    "const f=[];"
    "for(const d of p){try{"
    "const g=JSON.parse(d),h=g.message;"
    "if(!h||![\"user\",\"assistant\"].includes(h.role))continue;"
    "const v=Array.isArray(h.content)?"
    "h.content.filter(b=>b.type===\"text\").map(b=>b.text).join(\"\"):"
    "typeof h.content===\"string\"?h.content:\"\";"
    "if(v.trim())f.push(`${h.role.toUpperCase()}: ${v.trim()}`)"
    "}catch(d){}}"
    "if(f.length<3){console.log(\"[Agent] Compact: not enough messages to summarize\");return null}"
    "const m=f.slice(-80).join(\"\\n\\n\");"
    "console.log(`[Agent] Compact: summarizing ${f.length} messages, ${m.length} chars`);"
    # ── AI Gateway call (OpenAI-compatible format, correct model name) ───────
    "const _gwUrl=\"https://ai-gateway.happycapy.ai/api/v1/chat/completions\";"
    "const _gwKey=process.env.AI_GATEWAY_API_KEY||_e.get(\"AI_GATEWAY_API_KEY\",\"\");"
    "if(!_gwKey){console.error(\"[Agent] Compact: AI_GATEWAY_API_KEY not available\");return null}"
    "const resp=await fetch(_gwUrl,{"
    "method:\"POST\","
    "headers:{\"Authorization\":`Bearer ${_gwKey}`,\"Content-Type\":\"application/json\"},"
    "body:JSON.stringify({"
    "model:\"anthropic/claude-haiku-4.5\",max_tokens:800,"
    "messages:[{role:\"user\",content:"
    "`You are helping preserve context when a user switches AI models mid-conversation. "
    "Summarize this conversation concisely (under 600 words). Capture: what the user is working on, "
    "key decisions and outcomes, current state of ongoing tasks, important file names or code details, "
    "and the last request made.\\n\\nConversation:\\n${m.slice(0,12000)}\\n\\nSummary:`"
    "}]})});"
    "if(!resp.ok){console.error(`[Agent] Compact API error: ${resp.status} ${await resp.text().catch(()=>\"\")}`);return null}"
    "const data=await resp.json();"
    "const summary=data.choices?.[0]?.message?.content||data.content?.[0]?.text;"
    "if(!summary){console.error(\"[Agent] Compact: no summary in response\",JSON.stringify(data).slice(0,200));return null}"
    "console.log(`[Agent] Compact summary generated: ${summary.length} chars`);"
    "const _fromModel=r||\"previous model\";"
    "return`[Context bridged from prior conversation - model was switched from ${_fromModel} to ${s}]\\n\\n${summary}\\n\\n[End of bridged context - continue naturally]`"
    "}catch(t){console.error(\"[Agent] Compact generation failed:\",t);return null}"
    "})();"
    "this.sessionContextSeeds.set(e,o)}"
    # ── _buildJSONLPath ──────────────────────────────────────────────────────
    "_buildJSONLPath(e,i){"
    "try{"
    "const n=require(\"path\"),r=require(\"os\");"
    "const s=e.replace(/\\//g,\"-\");"
    "const a=n.join(r.homedir(),\".claude\",\"projects\",s);"
    "return n.join(a,`${i}.jsonl`)"
    "}catch(e){return null}}"
    # ── end of inserted methods ──────────────────────────────────────────────
    "detectModelProviderChange"
)

PATCHES = [PATCH_1, PATCH_2, PATCH_3, PATCH_4, (
    "_scheduleCompact and _buildJSONLPath methods",
    PATCH_5_DETECTION,
    PATCH_5_OLD,
    PATCH_5_NEW,
)]

# ─── Legacy patch detection strings (for migration from older versions) ──────
OLD_LEGACY_DETECTIONS = [
    "minimax->claude switch",
]


# ─────────────────────────────────────────────────────────────────────────────
# Server patching
# ─────────────────────────────────────────────────────────────────────────────

def check_patches(content):
    """Returns list of (patch, already_applied) tuples."""
    results = []
    for patch in PATCHES:
        name, detection, old, new = patch
        applied = detection in content
        results.append((patch, applied))
    return results


def has_legacy_patches(content):
    """Check if legacy patches are present (need migration)."""
    return any(det in content for det in OLD_LEGACY_DETECTIONS)


def revert_to_backup():
    """Revert server file to pre-patch backup for clean re-application."""
    backup_path = SERVER_JS + ".minimax-fix-bak"
    if os.path.exists(backup_path):
        shutil.copy(backup_path, SERVER_JS)
        ok(f"Reverted to original backup: {backup_path}")
        return True
    return False


def apply_server_patches(dry_run=False):
    print(bold("\n[1/2] Server patches"))
    print(f"      Target: {SERVER_JS}")

    if not os.path.exists(SERVER_JS):
        fail(f"Server file not found: {SERVER_JS}")
        return False

    with open(SERVER_JS, "r", errors="replace") as f:
        content = f.read()

    # Check for legacy patches that need migration
    if has_legacy_patches(content):
        print(yellow("  Legacy patches detected — migrating..."))
        if dry_run:
            info("Would revert to backup and re-apply patches")
        else:
            if not revert_to_backup():
                fail("No backup found to revert from. Cannot migrate.")
                fail("Manually restore the original server file and re-run.")
                return False
            # Re-read the clean file
            with open(SERVER_JS, "r", errors="replace") as f:
                content = f.read()

    patch_statuses = check_patches(content)
    pending = [(p, applied) for p, applied in patch_statuses if not applied]
    already_done = [(p, applied) for p, applied in patch_statuses if applied]

    for patch, _ in already_done:
        skip(f"Already applied: {patch[0]}")

    if not pending:
        ok("All patches already applied \u2014 nothing to do")
        return True

    if dry_run:
        for patch, _ in pending:
            info(f"Would apply: {patch[0]}")
        return True

    # Backup (only if not already backed up)
    backup_path = SERVER_JS + ".minimax-fix-bak"
    if not os.path.exists(backup_path):
        shutil.copy(SERVER_JS, backup_path)
        info(f"Backup created: {backup_path}")

    # Apply each pending patch
    errors = []
    for patch, _ in pending:
        name, detection, old, new = patch
        count = content.count(old)
        if count == 0:
            fail(f"Target string not found for: {name}")
            errors.append(name)
            continue
        if count > 1:
            fail(f"Ambiguous match ({count} occurrences) for: {name}")
            errors.append(name)
            continue
        content = content.replace(old, new, 1)
        ok(f"Applied: {name}")

    if errors:
        fail(f"{len(errors)} patch(es) failed \u2014 server NOT restarted")
        print(f"\n  The server file may be a different version than expected.")
        print(f"  Partial patches written \u2014 restore backup if needed: {backup_path}")
        with open(SERVER_JS, "w") as f:
            f.write(content)
        return False

    with open(SERVER_JS, "w") as f:
        f.write(content)

    ok(f"All {len(pending)} patch(es) written to {SERVER_JS}")
    return True


def restart_server(dry_run=False):
    print(bold("\n[2/2] Server restart"))

    if dry_run:
        info("Would restart: supervisorctl restart express_server")
        return True

    try:
        result = subprocess.run(
            ["sudo", "supervisorctl", "restart", "express_server"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            ok("Server restarted via supervisorctl")
            return True
    except Exception:
        pass

    # Fallback: find node process and send HUP
    try:
        result = subprocess.run(
            ["pgrep", "-f", "node dist/index.js"],
            capture_output=True, text=True
        )
        pid = result.stdout.strip()
        if pid:
            os.kill(int(pid.split()[0]), 1)  # SIGHUP
            ok(f"Server reloaded via SIGHUP (PID {pid.split()[0]})")
            return True
    except Exception:
        pass

    fail("Could not restart server \u2014 try manually: sudo supervisorctl restart express_server")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Session repair
# ─────────────────────────────────────────────────────────────────────────────

def find_session_jsonl_files(session_id):
    """Find all JSONL files associated with a session workspace."""
    workspace_path = WORKSPACE_BASE / session_id / "workspace"
    project_key = str(workspace_path).replace("/", "-")
    project_dir = CLAUDE_PROJECTS / project_key

    if not project_dir.exists():
        return []

    return list(project_dir.glob("*.jsonl"))


def repair_session(session_id, dry_run=False):
    print(bold(f"\n[Session repair] {session_id}"))

    jsonl_files = find_session_jsonl_files(session_id)

    if not jsonl_files:
        fail(f"No JSONL files found for session {session_id}")
        print(f"  Looked in: {CLAUDE_PROJECTS}/*{session_id}*/")
        return False

    info(f"Found {len(jsonl_files)} JSONL file(s)")

    total_removed = 0
    total_modified = 0

    for jsonl_path in jsonl_files:
        removed, modified = repair_jsonl(jsonl_path, dry_run)
        total_removed += removed
        total_modified += modified

    if total_removed == 0:
        ok("No invalid thinking blocks found \u2014 session is already clean")
    elif dry_run:
        info(f"Would remove {total_removed} invalid thinking blocks from {total_modified} message(s)")
    else:
        ok(f"Removed {total_removed} invalid thinking block(s) from {total_modified} message(s)")

    return True


def repair_jsonl(jsonl_path, dry_run=False):
    """
    Strip thinking blocks with short/invalid signatures from a JSONL file.
    Valid Anthropic signatures are ~1200 chars (base64 ECDSA).
    MiniMax signatures are 64-char hex hashes.
    """
    with open(jsonl_path, "r", errors="replace") as f:
        lines = f.readlines()

    fixed_lines = []
    removed = 0
    modified_messages = 0

    for line in lines:
        try:
            obj = json.loads(line)
            msg = obj.get("message", {})
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    new_content = []
                    changed = False
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "thinking":
                            sig = block.get("signature", None)
                            if not sig or len(sig) < 200:
                                removed += 1
                                changed = True
                                continue
                        new_content.append(block)

                    if changed:
                        msg["content"] = new_content
                        obj["message"] = msg
                        modified_messages += 1

            fixed_lines.append(json.dumps(obj) + "\n")
        except json.JSONDecodeError:
            fixed_lines.append(line)

    if removed > 0:
        rel = os.path.relpath(jsonl_path, Path.home())
        if dry_run:
            info(f"  ~/{rel}: would remove {removed} block(s) from {modified_messages} message(s)")
        else:
            backup = str(jsonl_path) + ".minimax-fix-bak"
            if not os.path.exists(backup):
                shutil.copy(jsonl_path, backup)
            with open(jsonl_path, "w") as f:
                f.writelines(fixed_lines)
            ok(f"  ~/{rel}: removed {removed} block(s) (backup: {os.path.basename(backup)})")

    return removed, modified_messages


# ─────────────────────────────────────────────────────────────────────────────
# Watcher install / uninstall
# ─────────────────────────────────────────────────────────────────────────────

def watcher_is_installed():
    if not os.path.exists(SUPERVISORD_CONF):
        return False
    with open(SUPERVISORD_CONF, "r") as f:
        return f"[{WATCHER_SECTION}]" in f.read()


def install_watcher(dry_run=False):
    print(bold("\n[Auto-reapply] Install supervisord watcher"))

    if watcher_is_installed():
        skip("Watcher already installed in supervisord.conf")
        return True

    if not os.path.exists(SUPERVISORD_CONF):
        fail(f"supervisord.conf not found: {SUPERVISORD_CONF}")
        return False

    if dry_run:
        info(f"Would append [{WATCHER_SECTION}] to {SUPERVISORD_CONF}")
        info("Would run: sudo supervisorctl update")
        return True

    try:
        result = subprocess.run(
            ["sudo", "tee", "-a", SUPERVISORD_CONF],
            input=WATCHER_CONF_BLOCK,
            capture_output=True, text=True
        )
        if result.returncode != 0:
            fail(f"Could not write to {SUPERVISORD_CONF}: {result.stderr.strip()}")
            return False
        ok(f"Appended [{WATCHER_SECTION}] to {SUPERVISORD_CONF}")

        result = subprocess.run(
            ["sudo", "supervisorctl", "update"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            ok("supervisorctl update \u2014 watcher started")
        else:
            info(f"supervisorctl update output: {result.stdout.strip()} {result.stderr.strip()}")

        return True
    except Exception as e:
        fail(f"Failed to install watcher: {e}")
        return False


def uninstall_watcher(dry_run=False):
    print(bold("\n[Auto-reapply] Uninstall supervisord watcher"))

    if not watcher_is_installed():
        skip("Watcher not installed \u2014 nothing to remove")
        return True

    if dry_run:
        info(f"Would remove [{WATCHER_SECTION}] block from {SUPERVISORD_CONF}")
        info("Would run: sudo supervisorctl stop minimax-fix-watcher && supervisorctl update")
        return True

    try:
        with open(SUPERVISORD_CONF, "r") as f:
            content = f.read()

        import re
        pattern = re.compile(
            r'\n\[' + re.escape(WATCHER_SECTION) + r'\][^\[]*',
            re.DOTALL
        )
        new_content = pattern.sub("", content)

        result = subprocess.run(
            ["sudo", "tee", SUPERVISORD_CONF],
            input=new_content,
            capture_output=True, text=True
        )
        if result.returncode != 0:
            fail(f"Could not write {SUPERVISORD_CONF}: {result.stderr.strip()}")
            return False
        ok(f"Removed [{WATCHER_SECTION}] from {SUPERVISORD_CONF}")

        subprocess.run(
            ["sudo", "supervisorctl", "stop", "minimax-fix-watcher"],
            capture_output=True, text=True, timeout=10
        )
        subprocess.run(
            ["sudo", "supervisorctl", "update"],
            capture_output=True, text=True, timeout=15
        )
        ok("Watcher stopped and removed from supervisord")
        return True
    except Exception as e:
        fail(f"Failed to uninstall watcher: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Status report
# ─────────────────────────────────────────────────────────────────────────────

def print_status():
    print(bold("\n[Status check]"))
    print(f"  File: {SERVER_JS}")

    if not os.path.exists(SERVER_JS):
        fail("Server file not found")
        return

    with open(SERVER_JS, "r", errors="replace") as f:
        content = f.read()

    # Check for legacy patches
    if has_legacy_patches(content):
        print(yellow("\n  WARNING: Legacy patches detected!"))
        print(yellow("  Run without --status to migrate (will revert backup and re-apply)."))
        print()

    patch_statuses = check_patches(content)
    all_applied = all(applied for _, applied in patch_statuses)

    for patch, applied in patch_statuses:
        name = patch[0]
        if applied:
            ok(name)
        else:
            fail(f"NOT applied: {name}")

    print()
    if all_applied:
        print(green("  All patches are active. Context is preserved in both directions."))
    else:
        missing = sum(1 for _, applied in patch_statuses if not applied)
        print(yellow(f"  {missing} patch(es) missing. Run without --dry-run to apply them."))

    # Watcher status
    print()
    print(bold("  [Auto-reapply watcher]"))
    if watcher_is_installed():
        ok("minimax-fix-watcher installed in supervisord.conf")
        try:
            result = subprocess.run(
                ["sudo", "supervisorctl", "status", "minimax-fix-watcher"],
                capture_output=True, text=True, timeout=5
            )
            status_line = result.stdout.strip()
            if "RUNNING" in status_line:
                ok(f"  Running: {status_line}")
            else:
                skip(f"  Not running: {status_line}")
        except Exception:
            info("  (Could not check runtime status)")
    else:
        info("  Not installed. Run with --install-watcher to enable auto-reapply on redeploy.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fix model switching bugs in HappyCapy (preserves context both directions)"
    )
    parser.add_argument(
        "--session", "-s",
        metavar="SESSION_ID",
        help="Also repair a specific stuck session (UUID, e.g. 983d37aa-f6f8-...)"
    )
    parser.add_argument(
        "--session-only",
        action="store_true",
        help="Only repair the session, skip server patches"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check what would be done without making any changes"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show patch status and exit"
    )
    parser.add_argument(
        "--install-watcher",
        action="store_true",
        help="Install supervisord eventlistener that auto-reapplies patches after redeploy"
    )
    parser.add_argument(
        "--uninstall-watcher",
        action="store_true",
        help="Remove the auto-reapply supervisord eventlistener"
    )
    args = parser.parse_args()

    print(bold("\n=== happycapy-model-switch ==="))

    if args.status:
        print_status()
        return 0

    if args.install_watcher:
        ok_w = install_watcher(dry_run=args.dry_run)
        print()
        if ok_w:
            print(green("Watcher installed. Patches will auto-reapply after any redeploy."))
        else:
            print(red("Watcher install failed. Check output above."))
            return 1
        return 0

    if args.uninstall_watcher:
        ok_w = uninstall_watcher(dry_run=args.dry_run)
        print()
        if ok_w:
            print(green("Watcher uninstalled."))
        else:
            print(red("Watcher uninstall failed. Check output above."))
            return 1
        return 0

    if args.dry_run:
        print(yellow("  (dry-run mode \u2014 no changes will be made)\n"))

    success = True

    # Server patches
    if not args.session_only:
        patched = apply_server_patches(dry_run=args.dry_run)
        if patched and not args.dry_run:
            restart_server(dry_run=args.dry_run)
        elif not patched:
            success = False

    # Session repair
    if args.session:
        repaired = repair_session(args.session, dry_run=args.dry_run)
        if not repaired:
            success = False

    print()
    if success:
        if args.dry_run:
            print(green("Dry-run complete. Run without --dry-run to apply."))
        else:
            print(green("Done. Model switching now preserves context in both directions."))
            if not args.session_only:
                print("  Behavior after fix:")
                print("  \u2022 MiniMax \u2192 Claude: Claude gets a compact summary of the prior conversation")
                print("  \u2022 Claude \u2192 MiniMax: MiniMax retains full server-side message history")
                print("  \u2022 No more 'Invalid signature in thinking block' errors")
    else:
        print(red("Completed with errors. Check output above."))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
