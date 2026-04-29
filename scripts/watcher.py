#!/usr/bin/env python3
"""
minimax-fix-watcher — polling program for supervisord
------------------------------------------------------
Polls every 30 seconds. If patches are missing from the server JS
(e.g. after a redeploy), re-applies them automatically.

Runs as a supervisord [program]. Logs to stderr.
"""

import subprocess
import sys
import time
from pathlib import Path

PATCH_SCRIPT = str(Path(__file__).parent / "patch.py")
SERVER_JS = "/app/server/dist/index.js"
POLL_INTERVAL = 30  # seconds

DETECTION_STRINGS = [
    "sessionContextSeeds=new Map",
    "Failed to clear persisted sdkSessionId",
    "s.model.toLowerCase().includes(\"claude\")",
    "Injected compact context seed for session",
    "ai-gateway.happycapy.ai/api/v1/chat/completions",
]


def log(msg):
    sys.stderr.write(f"[minimax-fix-watcher] {msg}\n")
    sys.stderr.flush()


def patches_applied():
    try:
        with open(SERVER_JS, "r", errors="replace") as f:
            content = f.read()
        return all(d in content for d in DETECTION_STRINGS)
    except Exception as e:
        log(f"Could not read server file: {e}")
        return True  # assume present if can't read


def main():
    log("Started — polling every 30s for missing patches")

    while True:
        if not patches_applied():
            log("Patches missing — re-applying...")
            result = subprocess.run(
                ["python3", PATCH_SCRIPT],
                capture_output=True,
                text=True,
            )
            for line in (result.stdout + result.stderr).splitlines():
                log(f"  {line}")
            if result.returncode == 0:
                log("Patches re-applied successfully")
            else:
                log("ERROR: patch script failed")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
