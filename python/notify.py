#!/usr/bin/env python3
"""Send a message from any terminal/agent inside Rex to its Telegram Topic."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HOOK_URL = os.environ.get("REX_TG_HOOK_URL", "http://127.0.0.1:18790/hook")


def main() -> int:
    p = argparse.ArgumentParser(description="Push message to rex-tg-bridge")
    p.add_argument("message", nargs="?", help="Message text (or read from stdin)")
    p.add_argument("--agent", default="terminal")
    p.add_argument("--title", default="")
    p.add_argument("--status", default="done", choices=["done", "blocked", "question", "error", "info"])
    p.add_argument("-s", "--session", default=os.environ.get("REX_SESSION", ""))
    p.add_argument("-b", "--block", default=os.environ.get("REX_BLOCK", ""))
    args = p.parse_args()

    text = args.message if args.message is not None else sys.stdin.read()
    if not (args.session or args.block):
        print("rex-tg-notify: neither REX_SESSION nor REX_BLOCK is set", file=sys.stderr)
        return 1
    payload = {
        "agent": args.agent,
        "event": "notify",
        "status": args.status,
        "title": args.title,
        "session_id": args.session,
        "block_id": args.block,
        "text": text.strip(),
        "kb_kind": "blocked" if args.status == "blocked" else ("question" if args.status == "question" else "default"),
    }
    req = urllib.request.Request(
        HOOK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        print(resp.read().decode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
