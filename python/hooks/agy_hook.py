#!/usr/bin/env python3
"""Antigravity CLI (agy) hook adapter for rex-tg-bridge. Always fails open."""
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
        # Never go through HTTP_PROXY for localhost
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=2.5):
            pass
    except Exception:
        pass


def extract_last_turn_reply(transcript_path: str) -> str:
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    full_p = p.with_name("transcript_full.jsonl")
    target = full_p if full_p.exists() else p
    if not target.exists():
        return ""

    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""

    replies: list[str] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        t = obj.get("type")
        if t == "USER_INPUT":
            break
        if t == "PLANNER_RESPONSE":
            content = (obj.get("content") or "").strip()
            if content:
                replies.append(content)
    replies.reverse()
    return "\n\n".join(replies)


def main() -> None:
    raw = sys.stdin.read().strip()
    data = json.loads(raw) if raw else {}
    event_name = data.get("hookEventName", "")
    session_id = os.environ.get("REX_SESSION", "")
    block_id = os.environ.get("REX_BLOCK", "")

    if session_id or block_id:
        if event_name == "Stop":
            reason = data.get("terminationReason", "")
            err = (data.get("error") or "").strip()
            reply = extract_last_turn_reply(data.get("transcriptPath", ""))
            status = "error" if reason == "ERROR" or err else "done"
            text = reply
            if err:
                text = f"{reply}\n\n[Error] {err}".strip()
            if not text:
                text = f"回合结束 ({reason or 'done'})"
            cwd = data.get("cwd", "")
            title = os.path.basename(cwd) if cwd else ""
            post_hook(
                {
                    "agent": "agy",
                    "event": "Stop",
                    "status": status,
                    "title": title,
                    "session_id": session_id,
                    "block_id": block_id,
                    "text": text,
                    "kb_kind": "default",
                }
            )
        elif event_name == "PreToolUse":
            tool_call = data.get("toolCall") or {}
            if tool_call.get("name") == "ask_question":
                args = tool_call.get("args") or {}
                questions = args.get("questions") or []
                lines: list[str] = []
                for qi, q in enumerate(questions, start=1):
                    lines.append(f"Q{qi}: {q.get('question', '')}")
                    for oi, opt in enumerate(q.get("options") or [], start=1):
                        lines.append(f"  {oi}. {opt}")
                if lines:
                    post_hook(
                        {
                            "agent": "agy",
                            "event": "ask_question",
                            "status": "question",
                            "title": "需要你选择",
                            "session_id": session_id,
                            "block_id": block_id,
                            "text": "\n".join(lines),
                            "kb_kind": "question",
                        }
                    )

    if event_name == "PreToolUse":
        print(json.dumps({"decision": "allow"}))
    else:
        print("{}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
