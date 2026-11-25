#!/usr/bin/env python3
"""Codex CLI notify adapter for rex-tg-bridge."""
from __future__ import annotations

import json
import os
import sys
import urllib.request

HOOK_URL = os.environ.get("REX_TG_HOOK_URL", "http://127.0.0.1:18790/hook")


def main() -> None:
    session_id = os.environ.get("REX_SESSION", "")
    block_id = os.environ.get("REX_BLOCK", "")
    if not (session_id or block_id) or len(sys.argv) < 2:
        return
    try:
        data = json.loads(sys.argv[1])
    except Exception:
        return
    text = (data.get("last-assistant-message") or "").strip()
    if not text:
        return
    cwd = data.get("cwd", "")
    payload = {
        "agent": "codex",
        "event": data.get("type", "agent-turn-complete"),
        "status": "done",
        "title": os.path.basename(cwd) if cwd else "",
        "session_id": session_id,
        "block_id": block_id,
        "text": text,
        "kb_kind": "default",
    }
    try:
        req = urllib.request.Request(
            HOOK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=2.5):
            pass
    except Exception:
        pass


if __name__ == "__main__":
    main()
