#!/usr/bin/env python3
"""Claude Code hook adapter (Stop / Notification / PermissionRequest) for rex-tg-bridge."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

HOOK_URL = os.environ.get("REX_TG_HOOK_URL", "http://127.0.0.1:18790/hook")


def post_hook(payload: dict) -> None:
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


def extract_claude_last_reply(transcript_path: str) -> str:
    if not transcript_path or not Path(transcript_path).exists():
        return ""
    lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    for raw in reversed(lines):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                joined = "\n".join(p.strip() for p in parts if p.strip())
                if joined:
                    return joined
    return ""


def main() -> None:
    session_id = os.environ.get("REX_SESSION", "")
    block_id = os.environ.get("REX_BLOCK", "")
    if not (session_id or block_id):
        return
    raw = sys.stdin.read().strip()
    data = json.loads(raw) if raw else {}
    ev = data.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "Stop")
    cwd = data.get("cwd", "")
    title = os.path.basename(cwd) if cwd else ""

    if ev == "Stop":
        text = (data.get("last_assistant_message") or "").strip() or extract_claude_last_reply(
            data.get("transcript_path", "")
        )
        post_hook(
            {
                "agent": "claude",
                "event": "Stop",
                "status": "done",
                "title": title,
                "session_id": session_id,
                "block_id": block_id,
                "text": text or "Claude 回合结束",
                "kb_kind": "default",
            }
        )
    elif ev == "PermissionRequest":
        tool = data.get("tool_name") or "tool"
        inp = json.dumps(data.get("tool_input") or {}, ensure_ascii=False)[:600]
        post_hook(
            {
                "agent": "claude",
                "event": "PermissionRequest",
                "status": "blocked",
                "title": f"请求授权: {tool}",
                "session_id": session_id,
                "block_id": block_id,
                "text": inp,
                "kb_kind": "blocked",
            }
        )
    elif ev == "Notification":
        post_hook(
            {
                "agent": "claude",
                "event": "Notification",
                "status": "blocked",
                "title": data.get("title") or "等待处理",
                "session_id": session_id,
                "block_id": block_id,
                "text": data.get("message") or "",
                "kb_kind": "blocked",
            }
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
