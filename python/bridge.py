#!/usr/bin/env python3
"""
rex-tg-bridge: 24/7 Telegram Forum Topics <-> Rex Server bridge for agy & general agents.
Zero third-party dependencies (Python 3.12 stdlib only).
"""

from __future__ import annotations

import html
import http.server
import json
import logging
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rex-tg-bridge")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_PROXY = os.environ.get("TG_PROXY", os.environ.get("HTTPS_PROXY", "")).strip()
TG_API_BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")
HOOK_HOST = os.environ.get("HOOK_HOST", "127.0.0.1")
HOOK_PORT = int(os.environ.get("HOOK_PORT", "18790"))
DEFAULT_STATE_FILE = Path.home() / ".local" / "state" / "rex-tg-bridge" / "state.json"
STATE_FILE = Path(os.environ.get("STATE_FILE", str(DEFAULT_STATE_FILE)))
REX_BIN = os.environ.get("REX_BIN", shutil.which("rex") or "rex")
CAPTURE_LINES = int(os.environ.get("CAPTURE_LINES", "45"))
HOOK_DEDUP_SEC = float(os.environ.get("HOOK_DEDUP_SEC", "12"))
HOOK_GRACE_SEC = float(os.environ.get("HOOK_GRACE_SEC", "1.3"))
BELL_COOLDOWN_SEC = float(os.environ.get("BELL_COOLDOWN_SEC", "15"))
NOTIFY_MODE = os.environ.get("NOTIFY_MODE", "smart").strip().lower()
if NOTIFY_MODE not in ("smart", "away", "always"):
    NOTIFY_MODE = "smart"
TG_ACTIVE_WINDOW_SEC = float(os.environ.get("TG_ACTIVE_WINDOW_SEC", "900"))
ALWAYS_PUSH_URGENT = os.environ.get("ALWAYS_PUSH_URGENT", "1").strip().lower() not in ("0", "false", "no", "off")
CLIENT_CACHE_TTL_SEC = float(os.environ.get("CLIENT_CACHE_TTL_SEC", "3.0"))

MAX_MSG_ROUTES = 2500
MAX_TG_CHUNK = 3600

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
DECORATIVE_LINE_RE = re.compile(r"^[\s─━═┄┅┈┉╌╍\-_=~╭╮╯╰┌┐└┘├┤┬┴┼│┃║|▀▄█░▒▓]+$")
PROMPT_ONLY_RE = re.compile(r"^[\s>❯›»$#]+$")
FOOTER_LINE_PATTERNS = [
    re.compile(r"^\?\s*for shortcuts\b", re.I),
    re.compile(r"^(?:accept-edits|plan|default|auto-edit|yolo)\s*[·•|]", re.I),
    re.compile(r"^Ctx\s+.*\d+(?:\.\d+)?%", re.I),
    re.compile(r"^Quota\s+", re.I),
    re.compile(r"^\d+(?:\.\d+)?%\(\d+[dhms]\)", re.I),
    re.compile(r"^(?:shift\+tab|ctrl\+[a-z]|esc)\s+to\s+", re.I),
    re.compile(r"^bypass permissions\b", re.I),
    re.compile(r"^⏵⏵\s*", re.I),
]
HOOK_CAPABLE_APPS = {"antigravity", "agy", "claude", "claude-code", "codex"}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def clean_tui_output(text: str, last_sent: str | None = None) -> str:
    """Remove TUI borders, status footers, empty prompts, and excess blank lines."""
    lines = [ln.rstrip() for ln in strip_ansi(text).splitlines()]

    # Drop trailing empty lines & known TUI bottom chrome first
    cleaned: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            cleaned.append("")
            continue
        if DECORATIVE_LINE_RE.match(s):
            continue
        if any(pat.search(s) for pat in FOOTER_LINE_PATTERNS):
            continue
        cleaned.append(ln)

    # Trim leading/trailing empty lines and standalone prompt markers (>, ❯, ›)
    while cleaned and (not cleaned[-1].strip() or PROMPT_ONLY_RE.match(cleaned[-1].strip())):
        cleaned.pop()
    while cleaned and (not cleaned[0].strip() or PROMPT_ONLY_RE.match(cleaned[0].strip())):
        cleaned.pop(0)

    # Strip echoed user input at the top (e.g., "> 1" right after user sent "1")
    if last_sent:
        sent_first = last_sent.strip().splitlines()[0].strip() if last_sent.strip() else ""
        if sent_first and cleaned:
            top = cleaned[0].strip()
            for prefix in ("> ", "❯ ", "› ", "» ", ""):
                if top == f"{prefix}{sent_first}":
                    cleaned.pop(0)
                    while cleaned and not cleaned[0].strip():
                        cleaned.pop(0)
                    break

    # Collapse multiple blank lines to at most 1
    out: list[str] = []
    blank_run = 0
    for ln in cleaned:
        if not ln.strip():
            blank_run += 1
            if blank_run <= 1:
                out.append("")
        else:
            blank_run = 0
            out.append(ln)
    return "\n".join(out).strip()


def md_to_tg_html(text: str) -> str:
    """Convert common agent Markdown into clean, safe Telegram HTML."""
    # 1) Protect fenced code blocks
    code_blocks: list[str] = []

    def _repl_code_block(m: re.Match[str]) -> str:
        lang = (m.group(1) or "").strip()
        code = m.group(2).strip("\n")
        escaped = html.escape(code)
        if lang and re.match(r"^[A-Za-z0-9_+#.\-]+$", lang):
            snippet = f'<pre><code class="language-{html.escape(lang)}">{escaped}</code></pre>'
        else:
            snippet = f"<pre>{escaped}</pre>"
        idx = len(code_blocks)
        code_blocks.append(snippet)
        return f"\x00CODEBLOCK{idx}\x00"

    text = re.sub(r"```([^\n`]*)\n(.*?)```", _repl_code_block, text, flags=re.S)

    # 2) Protect inline code
    inline_codes: list[str] = []

    def _repl_inline(m: re.Match[str]) -> str:
        idx = len(inline_codes)
        inline_codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00INLINECODE{idx}\x00"

    text = re.sub(r"`([^`\n]+)`", _repl_inline, text)

    # 3) Escape remaining HTML special chars
    text = html.escape(text)

    # 4) Links [label](https://...) and [label](file://...)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(file://[^)\s]+\)",
        lambda m: f"<code>{m.group(1)}</code>",
        text,
    )

    # 5) Line-level formatting (headings, bullets, hr, blockquotes)
    formatted_lines: list[str] = []
    for ln in text.splitlines():
        if re.match(r"^\s*[-*_]{3,}\s*$", ln):
            formatted_lines.append("───")
            continue
        hm = re.match(r"^\s*#{1,6}\s+(.+)$", ln)
        if hm:
            formatted_lines.append(f"<b>{hm.group(1).strip()}</b>")
            continue
        bm = re.match(r"^(\s*)[-*]\s+(.+)$", ln)
        if bm:
            indent = "  " * (len(bm.group(1)) // 2)
            formatted_lines.append(f"{indent}• {bm.group(2)}")
            continue
        qm = re.match(r"^\s*&gt;\s?(.*)$", ln)
        if qm:
            formatted_lines.append(f"│ <i>{qm.group(1)}</i>")
            continue
        formatted_lines.append(ln)
    text = "\n".join(formatted_lines)

    # 6) Inline bold **...** and ~~...~~
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 7) Restore placeholders
    for idx, snippet in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{idx}\x00", snippet)
    for idx, snippet in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{idx}\x00", snippet)
    return text


def split_chunks(text: str, limit: int = MAX_TG_CHUNK) -> list[str]:
    text = text.strip("\n")
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if cur:
                chunks.append("".join(cur).rstrip("\n"))
                cur, cur_len = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        if cur_len + len(line) > limit and cur:
            chunks.append("".join(cur).rstrip("\n"))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line)
    if cur:
        chunks.append("".join(cur).rstrip("\n"))
    return [c for c in chunks if c]


def compute_capture_diff(prev: str | None, curr: str, fallback_tail: int = 35) -> str:
    curr_lines = [ln.rstrip() for ln in curr.splitlines()]
    while curr_lines and not curr_lines[-1]:
        curr_lines.pop()
    if not curr_lines:
        return ""
    if not prev:
        return "\n".join(curr_lines[-fallback_tail:])
    prev_lines = [ln.rstrip() for ln in prev.splitlines()]
    while prev_lines and not prev_lines[-1]:
        prev_lines.pop()
    if not prev_lines:
        return "\n".join(curr_lines[-fallback_tail:])

    anchor_len = min(6, len(prev_lines))
    anchor = prev_lines[-anchor_len:]
    match_idx = -1
    for i in range(len(curr_lines) - anchor_len, -1, -1):
        if curr_lines[i : i + anchor_len] == anchor:
            match_idx = i + anchor_len
            break
    if match_idx != -1 and match_idx < len(curr_lines):
        return "\n".join(curr_lines[match_idx:][-80:])

    common = 0
    for a, b in zip(prev_lines, curr_lines):
        if a == b:
            common += 1
        else:
            break
    if common < len(curr_lines):
        return "\n".join(curr_lines[common:][-60:])
    return "\n".join(curr_lines[-fallback_tail:])


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {
            "chat_id": None,
            "allowed_user_id": None,
            "sessions": {},
            "topics": {},
            "msg_routes": {},
        }
        self.last_captures: dict[str, str] = {}
        self.last_hook_ts: dict[str, float] = {}
        self.last_bell_ts: dict[str, float] = {}
        self.last_sent_text: dict[str, str] = {}
        self.last_tg_input_ts: dict[str, float] = {}
        self.load()

    def load(self) -> None:
        with self.lock:
            if not self.path.exists():
                return
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception as exc:
                log.warning("Failed to load state %s: %s", self.path, exc)

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def record_route(self, msg_id: int, session_id: str, block_id: str | None) -> None:
        with self.lock:
            routes: dict[str, Any] = self.data.setdefault("msg_routes", {})
            routes[str(msg_id)] = {
                "session_id": session_id,
                "block_id": block_id,
                "ts": int(time.time()),
            }
            if len(routes) > MAX_MSG_ROUTES:
                excess = len(routes) - MAX_MSG_ROUTES
                for k in list(routes.keys())[:excess]:
                    routes.pop(k, None)
            self.save()


class TelegramAPI:
    def __init__(self, token: str, proxy: str, api_base: str) -> None:
        self.token = token
        self.api_base = api_base
        handlers: list[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 40) -> Any:
        url = f"{self.api_base}/bot{self.token}/{method}"
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    if not data.get("ok"):
                        raise RuntimeError(f"Telegram API {method} failed: {data}")
                    return data.get("result")
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                try:
                    err_json = json.loads(err_body)
                except Exception:
                    err_json = {}
                if exc.code == 429:
                    retry_after = err_json.get("parameters", {}).get("retry_after", 3)
                    log.warning("Telegram 429 on %s, sleeping %ss", method, retry_after)
                    time.sleep(float(retry_after) + 0.5)
                    continue
                desc = err_json.get("description", err_body)
                raise RuntimeError(f"Telegram HTTP {exc.code} on {method}: {desc}") from exc
            except Exception as exc:
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Telegram call {method} exhausted retries")

    def send_html(
        self,
        chat_id: int,
        text_html: str,
        thread_id: int | None = None,
        reply_to: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        fallback_plain: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            return self.call("sendMessage", payload, timeout=25)
        except RuntimeError as exc:
            if "can't parse entities" in str(exc) and fallback_plain is not None:
                payload.pop("parse_mode", None)
                payload["text"] = fallback_plain
                return self.call("sendMessage", payload, timeout=25)
            raise

    def react(self, chat_id: int, message_id: int, emoji: str = "⚡") -> None:
        try:
            self.call(
                "setMessageReaction",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
                timeout=10,
            )
        except Exception:
            pass


class RexClient:
    def __init__(self, rex_bin: str = REX_BIN) -> None:
        self.rex_bin = rex_bin

    def _run(
        self,
        args: list[str],
        stdin_text: str | None = None,
        timeout: int = 15,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.rex_bin, *args],
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def _run_json(self, args: list[str], stdin_text: str | None = None, timeout: int = 15) -> Any:
        proc = self._run(args, stdin_text=stdin_text, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"rex {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        out = proc.stdout.strip()
        return json.loads(out) if out else {}

    def list_sessions(self) -> list[dict[str, Any]]:
        data = self._run_json(["ls", "--json"])
        if isinstance(data, dict):
            return data.get("sessions", [])
        return data if isinstance(data, list) else []

    def inspect_session(self, session_id: str) -> dict[str, Any]:
        return self._run_json(["session", "inspect", session_id, "--json"])

    def get_block_process(self, session_id: str, block_id: str) -> dict[str, Any]:
        try:
            return self._run_json(["block", "call", "-s", session_id, "-b", block_id, "process"], timeout=8)
        except Exception:
            return {}

    def list_session_blocks(self, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        view = self.inspect_session(session_id)
        active_win = view.get("active_window_id")
        blocks: list[dict[str, Any]] = []
        for idx, win in enumerate(view.get("windows", []), start=1):
            win_id = win.get("window_id")
            win_label = win.get("label") or f"tab-{idx}"
            focused_bid = win.get("focused_block_id")
            for layer in win.get("layers", []):
                for blk in layer.get("blocks", []):
                    bid = blk.get("block_id")
                    if not bid:
                        continue
                    proc = self.get_block_process(session_id, bid)
                    fg = proc.get("foreground") or proc.get("child") or {}
                    prog = fg.get("argv0") or fg.get("name") or blk.get("label") or "shell"
                    prog = os.path.basename(prog)
                    cwd = fg.get("cwd") or ""
                    blocks.append(
                        {
                            "block_id": bid,
                            "label": blk.get("label") or "",
                            "flavor": blk.get("flavor") or "",
                            "window_id": win_id,
                            "window_label": win_label,
                            "is_active_window": win_id == active_win,
                            "is_focused": (win_id == active_win and bid == focused_bid),
                            "program": prog,
                            "cwd": cwd,
                        }
                    )
        return view, blocks

    def resolve_default_block(self, session_id: str, preferred_block_id: str | None = None) -> str | None:
        view = self.inspect_session(session_id)
        all_bids: list[str] = []
        active_win = view.get("active_window_id")
        focused_bid: str | None = None
        for win in view.get("windows", []):
            if win.get("window_id") == active_win:
                focused_bid = win.get("focused_block_id")
            for layer in win.get("layers", []):
                for blk in layer.get("blocks", []):
                    bid = blk.get("block_id")
                    if bid:
                        all_bids.append(bid)
        if preferred_block_id and preferred_block_id in all_bids:
            return preferred_block_id
        if focused_bid and focused_bid in all_bids:
            return focused_bid
        return all_bids[0] if all_bids else None

    def capture(self, session_id: str, block_id: str, tail_lines: int | None = None) -> str:
        proc = self._run(["capture", "-s", session_id, "-b", block_id, "--trim", "--unwrap"], timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"capture failed ({proc.returncode})")
        text = strip_ansi(proc.stdout)
        if tail_lines is not None and tail_lines > 0:
            lines = text.splitlines()
            while lines and not lines[-1].strip():
                lines.pop()
            text = "\n".join(lines[-tail_lines:])
        return text

    def send_text(self, session_id: str, block_id: str, text: str, press_enter: bool = True) -> None:
        payload = json.dumps(
            {
                "block_id": block_id,
                "event": {"type": "paste", "payload": {"text": text}},
            },
            ensure_ascii=False,
        )
        proc = self._run(["api", "call", "-s", session_id, "block.event", "-"], stdin_text=payload, timeout=10)
        if proc.returncode != 0:
            fb = self._run(["send", "-s", session_id, "-b", block_id], stdin_text=text, timeout=10)
            if fb.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or fb.stderr.strip() or "send_text failed")
        if press_enter:
            time.sleep(0.08)
            self.send_keys(session_id, block_id, ["enter"])

    def send_keys(self, session_id: str, block_id: str, keys: list[str]) -> None:
        proc = self._run(["send-key", "-s", session_id, "-b", block_id, *keys], timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"send-key failed ({proc.returncode})")

    def create_session(self, label: str, cmd: str | None = None) -> dict[str, Any]:
        args = ["new", label, "--json"]
        if cmd:
            args.extend(["--", "bash", "-lc", f"{cmd}; exec bash -l"])
        return self._run_json(args, timeout=15)

    def rename_session(self, session_id: str, label: str) -> None:
        proc = self._run(["session", "rename", session_id, label], timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "rename failed")

    def kill_session(self, session_id: str) -> None:
        proc = self._run(["kill", session_id], timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "kill failed")

    def list_connected_app_clients(self) -> list[dict[str, Any]]:
        now = time.time()
        cache = getattr(self, "_app_clients_cache", None)
        if cache is not None and now - cache[0] < CLIENT_CACHE_TTL_SEC:
            return cache[1]
        try:
            res = self._run_json(["api", "call", "client.list", '{"kinds":["app"]}'], timeout=6)
            clients = res.get("clients") or []
            connected = [
                c for c in clients
                if ((c.get("info") or {}).get("kind") == "app")
                and ((c.get("connection_state") or "connected") == "connected")
            ]
            self._app_clients_cache = (now, connected)
            return connected
        except Exception as exc:
            log.debug("client.list failed: %s", exc)
            return []

    def invalidate_client_cache(self) -> None:
        self._app_clients_cache = None


class Bridge:
    def __init__(self, tg: TelegramAPI, rex: RexClient, store: StateStore) -> None:
        self.tg = tg
        self.rex = rex
        self.store = store
        self.stop_event = threading.Event()
        self.bot_username = ""
        self._session_locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            lk = self._session_locks.get(session_id)
            if lk is None:
                lk = threading.Lock()
                self._session_locks[session_id] = lk
            return lk

    def is_bound(self) -> bool:
        with self.store.lock:
            return bool(self.store.data.get("chat_id") and self.store.data.get("allowed_user_id"))

    def ensure_topic_for_session(self, session_id: str, label: str | None = None) -> int | None:
        with self._get_session_lock(session_id):
            with self.store.lock:
                chat_id = self.store.data.get("chat_id")
                if not chat_id:
                    return None
                sessions = self.store.data.setdefault("sessions", {})
                entry = sessions.get(session_id)
                if entry and entry.get("topic_id"):
                    tid = int(entry["topic_id"])
                    if entry.get("closed"):
                        try:
                            self.tg.call("reopenForumTopic", {"chat_id": chat_id, "message_thread_id": tid})
                        except Exception:
                            pass
                        entry["closed"] = False
                    if label and label != entry.get("label"):
                        self._rename_topic_locked(chat_id, tid, label)
                        entry["label"] = label
                    self.store.save()
                    return tid

            if not label:
                try:
                    view = self.rex.inspect_session(session_id)
                    label = view.get("label") or session_id[-8:]
                except Exception:
                    label = session_id[-8:]

            res = self.tg.call("createForumTopic", {"chat_id": chat_id, "name": label[:120]})
            tid = int(res["message_thread_id"])

            default_bid = None
            try:
                default_bid = self.rex.resolve_default_block(session_id)
            except Exception:
                pass

            with self.store.lock:
                self.store.data.setdefault("sessions", {})[session_id] = {
                    "topic_id": tid,
                    "label": label,
                    "active_block_id": default_bid,
                    "closed": False,
                }
                self.store.data.setdefault("topics", {})[str(tid)] = session_id
                self.store.save()

        try:
            _, blocks = self.rex.list_session_blocks(session_id)
            lines = [f"🖥 <b>{html.escape(label)}</b>"]
            for b in blocks:
                mark = "👉 " if b["block_id"] == default_bid else "• "
                cwd_short = b["cwd"].replace(str(Path.home()), "~", 1) if b["cwd"] else ""
                lines.append(
                    f"{mark}<b>{html.escape(b['program'])}</b> "
                    f"(<code>{html.escape(b['block_id'][-6:])}</code>) {html.escape(cwd_short)}"
                )
            lines.append("\n直接发消息输入给 Agent · /screen 截屏 · /panes 切换分屏")
            msg = self.tg.send_html(chat_id, "\n".join(lines), thread_id=tid, reply_markup=self._quick_kb("screen"))
            self.store.record_route(msg["message_id"], session_id, default_bid)
        except Exception as exc:
            log.warning("Failed sending welcome to topic %s: %s", tid, exc)
        return tid

    def _rename_topic_locked(self, chat_id: int, topic_id: int, label: str) -> None:
        try:
            self.tg.call(
                "editForumTopic",
                {"chat_id": chat_id, "message_thread_id": topic_id, "name": label[:120]},
            )
        except Exception as exc:
            if "TOPIC_NOT_MODIFIED" not in str(exc):
                log.warning("editForumTopic failed: %s", exc)

    def delete_topic_and_cleanup(self, session_id: str) -> bool:
        with self._get_session_lock(session_id):
            tids: set[int] = set()
            with self.store.lock:
                chat_id = self.store.data.get("chat_id")
                entry = self.store.data.get("sessions", {}).pop(session_id, None)
                if entry and entry.get("topic_id") is not None:
                    tids.add(int(entry["topic_id"]))
                topics = self.store.data.get("topics", {})
                for k in [k for k, v in topics.items() if v == session_id]:
                    topics.pop(k, None)
                    if k.isdigit():
                        tids.add(int(k))
                routes = self.store.data.get("msg_routes", {})
                for mid in [k for k, v in routes.items() if v.get("session_id") == session_id]:
                    routes.pop(mid, None)
                if not entry and not tids:
                    return False
                self.store.save()
            if chat_id:
                for tid in sorted(tids):
                    try:
                        self.tg.call("deleteForumTopic", {"chat_id": chat_id, "message_thread_id": tid})
                        log.info("Deleted forum topic %s for session %s", tid, session_id)
                    except Exception as exc:
                        log.warning("deleteForumTopic(%s) failed (cleaning up anyway): %s", tid, exc)
            return True

    def sync_all_sessions(self) -> tuple[int, int]:
        live = self.rex.list_sessions()
        live_ids = {s["session_id"] for s in live}
        created_or_active = 0
        for s in live:
            tid = self.ensure_topic_for_session(s["session_id"], s.get("label"))
            if tid is not None:
                created_or_active += 1
        deleted = 0
        with self.store.lock:
            stale_sids = [sid for sid in list(self.store.data.get("sessions", {}).keys()) if sid not in live_ids]
        for sid in stale_sids:
            if self.delete_topic_and_cleanup(sid):
                deleted += 1
        return created_or_active, deleted

    def _quick_kb(self, kind: str = "minimal") -> dict[str, Any] | None:
        if kind == "none":
            return None
        if kind == "blocked":
            return {
                "inline_keyboard": [
                    [
                        {"text": "✅ y", "callback_data": "k:y"},
                        {"text": "❌ n", "callback_data": "k:n"},
                        {"text": "1️⃣", "callback_data": "k:1"},
                        {"text": "2️⃣", "callback_data": "k:2"},
                        {"text": "3️⃣", "callback_data": "k:3"},
                    ],
                    [
                        {"text": "📸 截屏", "callback_data": "scr"},
                        {"text": "⏎ Enter", "callback_data": "k:enter"},
                        {"text": "🛑 Ctrl+C", "callback_data": "k:ctrl+c"},
                    ],
                ]
            }
        if kind == "question":
            return {
                "inline_keyboard": [
                    [
                        {"text": "1️⃣", "callback_data": "k:1"},
                        {"text": "2️⃣", "callback_data": "k:2"},
                        {"text": "3️⃣", "callback_data": "k:3"},
                        {"text": "4️⃣", "callback_data": "k:4"},
                    ],
                    [
                        {"text": "📸 截屏", "callback_data": "scr"},
                        {"text": "⎋ Esc", "callback_data": "k:esc"},
                        {"text": "🛑 Ctrl+C", "callback_data": "k:ctrl+c"},
                    ],
                ]
            }
        if kind == "screen":
            return {
                "inline_keyboard": [
                    [
                        {"text": "🔄 刷新截屏", "callback_data": "scr"},
                        {"text": "⏎ Enter", "callback_data": "k:enter"},
                        {"text": "🛑 Ctrl+C", "callback_data": "k:ctrl+c"},
                    ]
                ]
            }
        # minimal for regular clean replies
        return {
            "inline_keyboard": [
                [
                    {"text": "📸 查看终端", "callback_data": "scr"},
                    {"text": "🛑 Ctrl+C", "callback_data": "k:ctrl+c"},
                ]
            ]
        }

    # ---- Notification Policy (smart / away / always + per-topic mute/watch) ----

    def get_notify_mode(self) -> str:
        with self.store.lock:
            mode = self.store.data.get("notify_mode")
            if mode in ("smart", "away", "always"):
                return mode
        return NOTIFY_MODE

    def get_always_push_urgent(self) -> bool:
        with self.store.lock:
            val = self.store.data.get("always_push_urgent")
            if isinstance(val, bool):
                return val
        return ALWAYS_PUSH_URGENT

    def mark_tg_activity(self, session_id: str) -> None:
        with self.store.lock:
            self.store.last_tg_input_ts[session_id] = time.time()

    def should_push_outbound(self, session_id: str, status: str = "done") -> tuple[bool, str]:
        with self.store.lock:
            entry = self.store.data.get("sessions", {}).get(session_id) or {}
            override = entry.get("notify_override")
            last_tg_ts = self.store.last_tg_input_ts.get(session_id, 0.0)

        if override == "mute":
            return False, "topic_muted"
        if override == "watch":
            return True, "topic_watched"

        mode = self.get_notify_mode()
        if mode == "always":
            return True, "mode_always"

        if self.get_always_push_urgent() and status in ("blocked", "question", "error"):
            return True, "urgent"

        app_clients = self.rex.list_connected_app_clients()
        if not app_clients:
            return True, "app_away"

        if mode == "away":
            return False, "app_connected_away"

        # mode == "smart": push only if this topic was recently interacted with from Telegram
        if time.time() - last_tg_ts <= TG_ACTIVE_WINDOW_SEC:
            return True, "tg_active"
        return False, "app_connected_idle_in_tg"

    def _snapshot_block_async(self, session_id: str, block_id: str | None) -> None:
        if not block_id:
            return

        def _snap() -> None:
            try:
                self.store.last_captures[block_id] = self.rex.capture(session_id, block_id)
            except Exception:
                pass

        threading.Thread(target=_snap, daemon=True).start()

    def _mode_status_html_and_kb(self, sid: str | None = None) -> tuple[str, dict[str, Any]]:
        mode = self.get_notify_mode()
        urgent = self.get_always_push_urgent()
        apps = self.rex.list_connected_app_clients()
        if apps:
            names = ", ".join(
                (c.get("info") or {}).get("label")
                or (c.get("info") or {}).get("platform")
                or "app"
                for c in apps
            )
            app_line = f"🟢 在线 ({html.escape(names)})"
        else:
            app_line = "⚪ 离线（将自动推送全部话题）"

        mode_desc = {
            "smart": "🧠 <b>smart</b>（Rex App 在线时，仅推近期在 TG 发过消息的话题）",
            "away": "🌙 <b>away</b>（Rex App 在线时全部静音，断开后恢复）",
            "always": "📢 <b>always</b>（始终全部推送）",
        }[mode]

        lines = [
            "<b>通知推送策略</b>",
            f"• 当前模式：{mode_desc}",
            f"• 紧急提醒（审批/提问/报错）：<b>{'开启' if urgent else '关闭'}</b>",
            f"• Rex GUI 客户端：{app_line}",
        ]
        if sid:
            with self.store.lock:
                ov = (self.store.data.get("sessions", {}).get(sid) or {}).get("notify_override")
            ov_label = {"mute": "🔇 已静音 (/unmute 恢复)", "watch": "👁 强制关注 (/unmute 恢复)"}.get(
                ov, "跟随全局模式（可用 /mute 或 /watch 单独覆盖）"
            )
            lines.append(f"• 当前话题：{ov_label}")

        kb = {
            "inline_keyboard": [
                [
                    {"text": f"{'✅ ' if mode == 'smart' else ''}🧠 smart", "callback_data": "mode:smart"},
                    {"text": f"{'✅ ' if mode == 'away' else ''}🌙 away", "callback_data": "mode:away"},
                    {"text": f"{'✅ ' if mode == 'always' else ''}📢 always", "callback_data": "mode:always"},
                ],
                [
                    {
                        "text": f"🔔 紧急提醒：{'开' if urgent else '关'}（点击切换）",
                        "callback_data": "mode:urgent",
                    }
                ],
            ]
        }
        return "\n".join(lines), kb

    # ---- Outbound: Agent / Rex -> Telegram ----

    def push_to_topic(
        self,
        session_id: str,
        block_id: str | None,
        header_html: str,
        body_text: str,
        kb_kind: str = "minimal",
        as_code: bool = False,
        render_md: bool = True,
        from_hook: bool = False,
    ) -> None:
        if not self.is_bound():
            log.info("Bridge not bound yet; dropping push for %s", session_id)
            return

        tid = self.ensure_topic_for_session(session_id)
        if tid is None:
            return

        with self.store.lock:
            chat_id = int(self.store.data["chat_id"])
            if block_id:
                s_entry = self.store.data["sessions"].get(session_id)
                if s_entry is not None:
                    s_entry["active_block_id"] = block_id
                if from_hook:
                    self.store.last_hook_ts[block_id] = time.time()
                self.store.save()

        chunks = split_chunks(body_text, MAX_TG_CHUNK)
        if not chunks:
            chunks = ["(无正文输出)"]

        kb = self._quick_kb(kb_kind)
        for idx, chunk in enumerate(chunks):
            prefix = f"{header_html}\n" if idx == 0 else f"<i>(续 {idx + 1}/{len(chunks)})</i>\n"
            if as_code:
                msg_html = f"{prefix}<pre>{html.escape(chunk)}</pre>"
            elif render_md:
                msg_html = f"{prefix}{md_to_tg_html(chunk)}"
            else:
                msg_html = f"{prefix}{html.escape(chunk)}"
            markup = kb if idx == len(chunks) - 1 else None
            sent = self.tg.send_html(
                chat_id,
                msg_html,
                thread_id=tid,
                reply_markup=markup,
                fallback_plain=chunk,
            )
            self.store.record_route(sent["message_id"], session_id, block_id)

        if from_hook and block_id:
            def _snap() -> None:
                try:
                    self.store.last_captures[block_id] = self.rex.capture(session_id, block_id)
                except Exception:
                    pass
            threading.Thread(target=_snap, daemon=True).start()

    def resolve_session_for_block(self, block_id: str) -> str | None:
        with self.store.lock:
            for sid, entry in self.store.data.get("sessions", {}).items():
                if entry.get("active_block_id") == block_id and not entry.get("closed"):
                    return sid
        try:
            info = self.rex._run_json(["block", "inspect", block_id, "--json"], timeout=8)
            sid = (info.get("block") or {}).get("session_id") or info.get("session_id")
            if sid:
                return sid
        except Exception:
            pass
        try:
            for s in self.rex.list_sessions():
                sid = s["session_id"]
                view = self.rex.inspect_session(sid)
                for w in view.get("windows", []):
                    for l in w.get("layers", []):
                        for b in l.get("blocks", []):
                            if b.get("block_id") == block_id:
                                return sid
        except Exception:
            pass
        return None

    def handle_hook_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        session_id = (data.get("session_id") or "").strip()
        block_id = (data.get("block_id") or "").strip() or None
        agent = (data.get("agent") or "agent").strip()
        status = (data.get("status") or "done").strip()
        title = (data.get("title") or "").strip()
        text = (data.get("text") or "").strip()
        kb_kind = (data.get("kb_kind") or ("blocked" if status == "blocked" else "minimal")).strip()

        if not session_id and block_id:
            session_id = self.resolve_session_for_block(block_id) or ""

        if not session_id:
            return {"ok": False, "error": "missing session_id and unresolved block_id"}

        if block_id:
            with self.store.lock:
                self.store.last_hook_ts[block_id] = time.time()

        allowed, reason = self.should_push_outbound(session_id, status=status)
        if not allowed:
            log.info("Suppressed hook push for %s (%s): %s", session_id[-8:], status, reason)
            self._snapshot_block_async(session_id, block_id)
            return {"ok": True, "suppressed": reason}

        icon = {
            "done": "🤖",
            "blocked": "⏳",
            "question": "❓",
            "error": "❌",
            "info": "ℹ️",
        }.get(status, "🤖")
        title_part = f" · {html.escape(title)}" if title else ""
        header = f"{icon} <b>{html.escape(agent)}</b>{title_part}"

        threading.Thread(
            target=self.push_to_topic,
            args=(session_id, block_id, header, text, kb_kind, False, True, True),
            daemon=True,
        ).start()
        return {"ok": True}

    # ---- Rex Events Listener ----

    def _handle_program_status_delayed(self, sid: str, bid: str, rec: dict[str, Any]) -> None:
        state = rec.get("state")
        app = (rec.get("app") or "terminal").strip()
        msg = (rec.get("msg") or "").strip()
        kind = (rec.get("kind") or "").strip()

        # Give structured agent hooks (Stop / ask_question) a short head start to avoid noisy duplicate
        if state == "done" and app.lower() in HOOK_CAPABLE_APPS:
            time.sleep(HOOK_GRACE_SEC)
            if time.time() - self.store.last_hook_ts.get(bid, 0.0) < HOOK_DEDUP_SEC:
                return

        if time.time() - self.store.last_hook_ts.get(bid, 0.0) < HOOK_DEDUP_SEC:
            return

        allowed, reason = self.should_push_outbound(sid, status=state or "done")
        if not allowed:
            log.info("Suppressed program_status(%s) for %s: %s", state, sid[-8:], reason)
            self._snapshot_block_async(sid, bid)
            return

        curr = ""
        cleaned_diff = ""
        try:
            curr = self.rex.capture(sid, bid)
            raw_diff = compute_capture_diff(self.store.last_captures.get(bid), curr, fallback_tail=40)
            self.store.last_captures[bid] = curr
            cleaned_diff = clean_tui_output(raw_diff, last_sent=self.store.last_sent_text.get(bid))
        except Exception as exc:
            log.warning("capture during program_status failed: %s", exc)

        # Deduplicate msg vs cleaned_diff so we never repeat the same answer + TUI noise
        body = ""
        if msg and cleaned_diff:
            plain_msg = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", msg)
            norm_msg = re.sub(r"\s+", "", plain_msg)
            norm_diff = re.sub(r"\s+", "", cleaned_diff)
            if norm_msg == norm_diff or (norm_msg in norm_diff and len(norm_diff) <= len(norm_msg) + 25):
                body = msg
            elif norm_msg in norm_diff:
                body = cleaned_diff
            else:
                body = f"{msg}\n\n{cleaned_diff}"
        else:
            body = msg or cleaned_diff

        if not body:
            body = f"状态变更为 {state}"

        icon = {"done": "🤖", "blocked": "⏳", "error": "❌"}[state]
        app_disp = "agy" if app.lower() == "antigravity" else app
        sub = f" · {kind}" if kind else (" · 出错" if state == "error" else (" · 等待处理" if state == "blocked" else ""))
        header = f"{icon} <b>{html.escape(app_disp)}</b>{html.escape(sub)}"
        kb = "blocked" if state == "blocked" else "minimal"
        self.push_to_topic(sid, bid, header, body, kb_kind=kb, as_code=False, render_md=True, from_hook=False)

    def handle_rex_event(self, raw: dict[str, Any]) -> None:
        ev_type = raw.get("type")
        payload = raw.get("payload") or {}

        if ev_type in ("client_joined", "client_left", "client_changed"):
            self.rex.invalidate_client_cache()
            return

        if ev_type == "session_created":
            sess = payload.get("session") or {}
            sid = sess.get("session_id")
            if sid and self.is_bound():
                self.ensure_topic_for_session(sid, sess.get("label"))
            return

        if ev_type == "session_destroyed":
            sid = payload.get("session_id")
            if sid:
                self.delete_topic_and_cleanup(sid)
            return

        if ev_type != "session_event":
            return

        sid = payload.get("session_id")
        inner = payload.get("event") or {}
        inner_type = inner.get("type")
        inner_payload = inner.get("payload") or {}

        if inner_type in ("client_connected", "client_disconnected", "client_connection_changed"):
            self.rex.invalidate_client_cache()
            return

        if inner_type == "session_label_changed" and sid:
            new_label = inner_payload.get("label")
            if new_label and self.is_bound():
                self.ensure_topic_for_session(sid, new_label)
            return

        if inner_type != "block_event" or not sid:
            return

        if inner_payload.get("creator_name") != "com.superlogical.terminal":
            return
        bid = inner_payload.get("block_id")
        ev_name = inner_payload.get("name")
        ev_data = inner_payload.get("payload") or {}
        if not bid or not self.is_bound():
            return

        if time.time() - self.store.last_hook_ts.get(bid, 0.0) < HOOK_DEDUP_SEC:
            return

        if ev_name == "program_status_changed":
            rec = ev_data.get("record") or {}
            if rec.get("state") in ("done", "blocked", "error"):
                threading.Thread(
                    target=self._handle_program_status_delayed,
                    args=(sid, bid, rec),
                    daemon=True,
                ).start()

        elif ev_name == "desktop_notification":
            allowed, reason = self.should_push_outbound(sid, status="info")
            if not allowed:
                log.debug("Suppressed desktop_notification for %s: %s", sid[-8:], reason)
                return
            title = ev_data.get("title") or "通知"
            body = ev_data.get("body") or ""
            header = f"🔔 <b>{html.escape(title)}</b>"
            self.push_to_topic(sid, bid, header, body, kb_kind="minimal", as_code=False, render_md=True)

        elif ev_name == "bell":
            now = time.time()
            if now - self.store.last_bell_ts.get(bid, 0.0) < BELL_COOLDOWN_SEC:
                return
            self.store.last_bell_ts[bid] = now
            allowed, reason = self.should_push_outbound(sid, status="info")
            if not allowed:
                log.debug("Suppressed bell for %s: %s", sid[-8:], reason)
                self._snapshot_block_async(sid, bid)
                return
            try:
                curr = self.rex.capture(sid, bid)
                raw_diff = compute_capture_diff(self.store.last_captures.get(bid), curr, fallback_tail=25)
                self.store.last_captures[bid] = curr
                cleaned = clean_tui_output(raw_diff, last_sent=self.store.last_sent_text.get(bid))
                if cleaned:
                    header = "🔔 <b>终端提醒</b>"
                    self.push_to_topic(sid, bid, header, cleaned, kb_kind="minimal", as_code=False, render_md=True)
            except Exception:
                pass

        elif ev_name == "child_exited":
            code = ev_data.get("exit_code")
            status = "error" if code not in (0, None) else "info"
            allowed, reason = self.should_push_outbound(sid, status=status)
            if not allowed:
                log.debug("Suppressed child_exited for %s: %s", sid[-8:], reason)
                return
            err = ev_data.get("error") or ""
            header = f"⏹ <b>终端进程退出</b> (code={code})"
            self.push_to_topic(sid, bid, header, err or f"exit_code={code}", kb_kind="none", render_md=False)

    def run_rex_events_loop(self) -> None:
        while not self.stop_event.is_set():
            proc: subprocess.Popen[str] | None = None
            try:
                proc = subprocess.Popen(
                    [self.rex.rex_bin, "events", "--all", "--json"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                log.info("Attached to rex events --all --json (pid=%s)", proc.pid)
                assert proc.stdout is not None
                for line in proc.stdout:
                    if self.stop_event.is_set():
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        self.handle_rex_event(ev)
                    except Exception as exc:
                        log.warning("Error handling rex event: %s", exc)
            except Exception as exc:
                log.warning("rex events stream error: %s", exc)
            finally:
                if proc and proc.poll() is None:
                    proc.terminate()
            if not self.stop_event.is_set():
                time.sleep(3)

    # ---- Inbound: Telegram -> Rex ----

    def _resolve_topic_session(self, msg: dict[str, Any]) -> tuple[str | None, int | None]:
        is_topic = bool(msg.get("is_topic_message"))
        tid = msg.get("message_thread_id") if is_topic else None
        if tid is None:
            return None, None
        with self.store.lock:
            sid = self.store.data.get("topics", {}).get(str(tid))
        return sid, int(tid)

    def _resolve_target(self, sid: str, reply_to_msg_id: int | None = None) -> str | None:
        preferred = None
        if reply_to_msg_id is not None:
            with self.store.lock:
                route = self.store.data.get("msg_routes", {}).get(str(reply_to_msg_id))
                if route and route.get("session_id") == sid and route.get("block_id"):
                    preferred = route["block_id"]
        if not preferred:
            with self.store.lock:
                s_entry = self.store.data.get("sessions", {}).get(sid) or {}
                preferred = s_entry.get("active_block_id")
        bid = self.rex.resolve_default_block(sid, preferred_block_id=preferred)
        if bid:
            with self.store.lock:
                s_entry = self.store.data.get("sessions", {}).get(sid)
                if s_entry is not None:
                    s_entry["active_block_id"] = bid
                    self.store.save()
        return bid

    def handle_telegram_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        sender = msg.get("from") or {}
        user_id = sender.get("id")
        if not chat_id or not user_id:
            return

        # Handle Telegram Topic service events (close -> kill Rex session & delete topic; edit -> rename Rex session)
        if self.is_bound() and not sender.get("is_bot"):
            with self.store.lock:
                bound_chat = self.store.data.get("chat_id")
                allowed_user = self.store.data.get("allowed_user_id")
            if chat_id == bound_chat and user_id == allowed_user:
                svc_tid = msg.get("message_thread_id")
                if "forum_topic_closed" in msg and svc_tid is not None:
                    with self.store.lock:
                        sid = self.store.data.get("topics", {}).get(str(svc_tid))
                    if sid:
                        log.info("Topic %s closed by user -> killing Rex session %s and deleting topic", svc_tid, sid)
                        try:
                            self.rex.kill_session(sid)
                        except Exception as exc:
                            log.warning("kill_session(%s) on topic close: %s", sid, exc)
                        self.delete_topic_and_cleanup(sid)
                    return
                if "forum_topic_edited" in msg and svc_tid is not None:
                    new_name = ((msg.get("forum_topic_edited") or {}).get("name") or "").strip()
                    if new_name:
                        with self.store.lock:
                            sid = self.store.data.get("topics", {}).get(str(svc_tid))
                            entry = self.store.data.get("sessions", {}).get(sid) if sid else None
                            if sid and entry and entry.get("label") != new_name:
                                entry["label"] = new_name
                                self.store.save()
                            else:
                                sid = None
                        if sid:
                            try:
                                self.rex.rename_session(sid, new_name)
                                log.info("Topic %s renamed by user -> renamed Rex session %s to %s", svc_tid, sid, new_name)
                            except Exception as exc:
                                log.warning("rename_session(%s) on topic edit failed: %s", sid, exc)
                    return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        is_topic = bool(msg.get("is_topic_message"))
        thread_id = msg.get("message_thread_id") if is_topic else None

        cmd = ""
        arg = ""
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].split("@", 1)[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

        if not self.is_bound():
            if cmd == "/bind":
                if chat.get("type") != "supergroup" or not chat.get("is_forum"):
                    self.tg.send_html(
                        chat_id,
                        "⚠️ 请在已开启 <b>Topics（话题）</b> 的私有超级群里发送 <code>/bind</code>，并确保 Bot 是管理员（勾选 Manage Topics）。",
                        thread_id=thread_id,
                    )
                    return
                with self.store.lock:
                    self.store.data["chat_id"] = chat_id
                    self.store.data["allowed_user_id"] = user_id
                    self.store.save()
                self.tg.send_html(
                    chat_id,
                    f"✅ <b>绑定成功！</b>\n"
                    f"• 群 ID: <code>{chat_id}</code>\n"
                    f"• 授权用户: <code>{user_id}</code>（已锁定仅你本人可操作）\n\n"
                    f"正在同步现有 Rex Sessions 为话题...",
                    thread_id=thread_id,
                )
                try:
                    active, closed = self.sync_all_sessions()
                    self.tg.send_html(
                        chat_id,
                        f"🎉 同步完成：已映射 <b>{active}</b> 个在线 Session 话题（删除过期 {closed} 个）。\n"
                        f"直接进入对应话题发消息即可与 Agent 对话！发送 /help 查看全部命令。",
                        thread_id=thread_id,
                    )
                except Exception as exc:
                    self.tg.send_html(
                        chat_id,
                        f"⚠️ 同步话题失败：<code>{html.escape(str(exc))}</code>\n"
                        f"请检查群管理员权限中是否已开启 <b>管理话题 (Manage Topics)</b>，开启后发送 <code>/sync</code> 即可。",
                        thread_id=thread_id,
                    )
            return

        with self.store.lock:
            bound_chat = self.store.data.get("chat_id")
            allowed_user = self.store.data.get("allowed_user_id")
        if user_id != allowed_user or chat_id != bound_chat:
            return

        sid, tid = self._resolve_topic_session(msg)
        reply_id = (msg.get("reply_to_message") or {}).get("message_id")

        if cmd == "/help":
            help_text = (
                "<b>Rex ↔ Telegram Topics 桥接命令</b>\n\n"
                "<b>全局命令（General 或任意话题）</b>\n"
                "• /mode <code>[smart|away|always]</code> — 查看/切换推送防打扰模式\n"
                "• /sync — 同步全部 Rex Sessions 为 Topics\n"
                "• /sessions 或 /ls — 查看全部在线 Session 与进程\n"
                "• /new <code>&lt;name&gt; [cmd]</code> — 新建 Session（例: <code>/new fix-api agy</code>）\n"
                "• /unbind — 解除当前群绑定\n\n"
                "<b>Session 话题内命令</b>\n"
                "• <i>直接发文字</i> — 粘贴并回车发送给当前 Agent（Reply 某条推送可精准回到原 pane）\n"
                "• /mute · /watch · /unmute — 静音 / 强制关注 / 恢复跟随全局模式\n"
                "• /screen <code>[行数]</code> 或 /s — 抓屏（默认 45 行，自动清洗 TUI 装饰线）\n"
                "• /rawscreen <code>[行数]</code> — 原始未清洗终端截屏\n"
                "• /panes — 查看并切换该 Session 下的分屏/Tab\n"
                "• /key <code>&lt;ctrl+c|esc|enter|...&gt;</code> — 发送按键\n"
                "• /raw <code>&lt;text&gt;</code> — 仅输入文本不回车\n"
                "• /rename <code>&lt;新名称&gt;</code> — 重命名 Session 与话题\n"
                "• /kill — 关闭当前 Rex Session（带确认）"
            )
            self.tg.send_html(chat_id, help_text, thread_id=thread_id)
            return

        if cmd == "/mode":
            if arg.lower() in ("smart", "away", "always"):
                new_mode = arg.lower()
                with self.store.lock:
                    self.store.data["notify_mode"] = new_mode
                    self.store.save()
            elif arg:
                self.tg.send_html(chat_id, "用法：<code>/mode smart</code> | <code>/mode away</code> | <code>/mode always</code>", thread_id=thread_id)
                return
            status_html, kb = self._mode_status_html_and_kb(sid)
            self.tg.send_html(chat_id, status_html, thread_id=thread_id, reply_markup=kb)
            return

        if cmd == "/unbind":
            with self.store.lock:
                self.store.data["chat_id"] = None
                self.store.data["allowed_user_id"] = None
                self.store.save()
            self.tg.send_html(chat_id, "🔓 已解除绑定。可在新群发送 <code>/bind</code> 重新绑定。", thread_id=thread_id)
            return

        if cmd == "/sync":
            try:
                active, closed = self.sync_all_sessions()
                self.tg.send_html(chat_id, f"🔄 同步完成：在线 <b>{active}</b> 个话题，删除过期 {closed} 个。", thread_id=thread_id)
            except Exception as exc:
                self.tg.send_html(
                    chat_id,
                    f"❌ 同步话题失败：<code>{html.escape(str(exc))}</code>\n"
                    f"请确认 Bot 管理员权限已勾选 <b>管理话题 (Manage Topics)</b>。",
                    thread_id=thread_id,
                )
            return

        if cmd in ("/sessions", "/ls"):
            sessions = self.rex.list_sessions()
            if not sessions:
                self.tg.send_html(chat_id, "当前没有运行中的 Rex Session。使用 <code>/new name agy</code> 创建一个。", thread_id=thread_id)
                return
            lines = ["<b>在线 Rex Sessions：</b>"]
            for s in sessions:
                s_id = s["session_id"]
                lbl = s.get("label") or s_id[-8:]
                try:
                    _, blks = self.rex.list_session_blocks(s_id)
                    progs = ", ".join(b["program"] for b in blks) or "empty"
                except Exception:
                    progs = "?"
                lines.append(f"• <b>{html.escape(lbl)}</b> (<code>{html.escape(s_id[-6:])}</code>) — {html.escape(progs)}")
            self.tg.send_html(chat_id, "\n".join(lines), thread_id=thread_id)
            return

        if cmd == "/new":
            if not arg:
                self.tg.send_html(chat_id, "用法：<code>/new &lt;session名称&gt; [启动命令]</code>\n例如：<code>/new task-1 agy</code>", thread_id=thread_id)
                return
            parts = arg.split(maxsplit=1)
            name = parts[0]
            launch_cmd = parts[1].strip() if len(parts) > 1 else None
            try:
                created = self.rex.create_session(name, launch_cmd)
                new_sid = created.get("session_id")
                if new_sid:
                    self.mark_tg_activity(new_sid)
                    self.ensure_topic_for_session(new_sid, created.get("label") or name)
                self.tg.send_html(chat_id, f"✅ 已创建 Session <b>{html.escape(name)}</b> 并开启对应话题。", thread_id=thread_id)
            except Exception as exc:
                self.tg.send_html(chat_id, f"❌ 创建失败：<code>{html.escape(str(exc))}</code>", thread_id=thread_id)
            return

        if not sid:
            if cmd:
                self.tg.send_html(
                    chat_id,
                    "ℹ️ 该命令需要在对应的 Session 话题内使用。发 /sync 同步话题，或发 /help 查看说明。",
                    thread_id=thread_id,
                )
            return

        if cmd in ("/mute", "/watch", "/unmute"):
            ov = None if cmd == "/unmute" else cmd.lstrip("/")
            with self.store.lock:
                entry = self.store.data.get("sessions", {}).get(sid)
                if entry is not None:
                    if ov is None:
                        entry.pop("notify_override", None)
                    else:
                        entry["notify_override"] = ov
                    self.store.save()
            reply_msg = {
                "mute": "🔇 已静音当前话题（仅手动 /screen 仍可用；发 /unmute 恢复）",
                "watch": "👁 已强制关注当前话题（即使 Rex App 在线也照常推送；发 /unmute 恢复）",
                None: "🔔 当前话题已恢复跟随全局推送模式（/mode 查看当前模式）",
            }[ov]
            self.tg.send_html(chat_id, reply_msg, thread_id=tid)
            return

        if cmd == "/panes":
            try:
                _, blks = self.rex.list_session_blocks(sid)
                cur_bid = self._resolve_target(sid)
                if not blks:
                    self.tg.send_html(chat_id, "该 Session 当前没有终端 Pane。", thread_id=tid)
                    return
                lines = ["<b>当前 Session 的 Panes：</b>"]
                kb_rows: list[list[dict[str, str]]] = []
                for idx, b in enumerate(blks, start=1):
                    mark = "✅ " if b["block_id"] == cur_bid else ""
                    short = b["block_id"][-8:]
                    cwd = b["cwd"].replace(str(Path.home()), "~", 1) if b["cwd"] else ""
                    lines.append(
                        f"{idx}. {mark}<b>{html.escape(b['program'])}</b> "
                        f"[{html.escape(b['window_label'])}] <code>{short}</code> {html.escape(cwd)}"
                    )
                    kb_rows.append(
                        [{"text": f"{'✅ ' if mark else ''}切换到 #{idx} {b['program']} ({short})", "callback_data": f"use:{short}"}]
                    )
                self.tg.send_html(chat_id, "\n".join(lines), thread_id=tid, reply_markup={"inline_keyboard": kb_rows})
            except Exception as exc:
                self.tg.send_html(chat_id, f"❌ 获取 Panes 失败：<code>{html.escape(str(exc))}</code>", thread_id=tid)
            return

        if cmd in ("/screen", "/s", "/rawscreen"):
            n = CAPTURE_LINES
            if arg.isdigit():
                n = max(5, min(200, int(arg)))
            bid = self._resolve_target(sid, reply_id)
            if not bid:
                self.tg.send_html(chat_id, "⚠️ 未找到可用的终端 block。", thread_id=tid)
                return
            try:
                full_cap = self.rex.capture(sid, bid)
                self.store.last_captures[bid] = full_cap
                if cmd == "/rawscreen":
                    lines = full_cap.splitlines()
                    content = "\n".join(lines[-n:])
                    header = f"📸 <b>原始截屏</b> (最后 {n} 行)"
                    self.push_to_topic(sid, bid, header, content or "(空白屏幕)", kb_kind="screen", as_code=True, render_md=False)
                else:
                    cleaned = clean_tui_output(full_cap)
                    lines = cleaned.splitlines()
                    content = "\n".join(lines[-n:])
                    header = f"📸 <b>终端内容</b>"
                    self.push_to_topic(sid, bid, header, content or "(空白屏幕)", kb_kind="screen", as_code=True, render_md=False)
            except Exception as exc:
                self.tg.send_html(chat_id, f"❌ 截屏失败：<code>{html.escape(str(exc))}</code>", thread_id=tid)
            return

        if cmd == "/key":
            if not arg:
                self.tg.send_html(chat_id, "用法：<code>/key ctrl+c</code> 或 <code>/key down enter</code>", thread_id=tid)
                return
            bid = self._resolve_target(sid, reply_id)
            if not bid:
                self.tg.send_html(chat_id, "⚠️ 未找到可用的终端 block。", thread_id=tid)
                return
            try:
                self.mark_tg_activity(sid)
                self.rex.send_keys(sid, bid, arg.split())
                self.tg.react(chat_id, msg["message_id"], "⚡")
            except Exception as exc:
                self.tg.send_html(chat_id, f"❌ 发送按键失败：<code>{html.escape(str(exc))}</code>", thread_id=tid)
            return

        if cmd == "/raw":
            if not arg:
                self.tg.send_html(chat_id, "用法：<code>/raw 要输入但不回车的文本</code>", thread_id=tid)
                return
            bid = self._resolve_target(sid, reply_id)
            if not bid:
                self.tg.send_html(chat_id, "⚠️ 未找到可用的终端 block。", thread_id=tid)
                return
            try:
                self.mark_tg_activity(sid)
                self.rex.send_text(sid, bid, arg, press_enter=False)
                self.tg.react(chat_id, msg["message_id"], "⚡")
            except Exception as exc:
                self.tg.send_html(chat_id, f"❌ 输入失败：<code>{html.escape(str(exc))}</code>", thread_id=tid)
            return

        if cmd == "/rename":
            if not arg:
                self.tg.send_html(chat_id, "用法：<code>/rename 新名称</code>", thread_id=tid)
                return
            try:
                self.rex.rename_session(sid, arg)
                self.ensure_topic_for_session(sid, arg)
                self.tg.send_html(chat_id, f"✏️ 已重命名为 <b>{html.escape(arg)}</b>", thread_id=tid)
            except Exception as exc:
                self.tg.send_html(chat_id, f"❌ 重命名失败：<code>{html.escape(str(exc))}</code>", thread_id=tid)
            return

        if cmd == "/kill":
            kb = {
                "inline_keyboard": [
                    [
                        {"text": "⚠️ 确认关闭此 Session", "callback_data": "kill_ok"},
                        {"text": "取消", "callback_data": "kill_no"},
                    ]
                ]
            }
            self.tg.send_html(chat_id, "确认关闭此 Rex Session 及其中的所有进程？", thread_id=tid, reply_markup=kb)
            return

        bid = self._resolve_target(sid, reply_id)
        if not bid:
            self.tg.send_html(chat_id, "⚠️ 当前 Session 没有活跃终端 block（可能已关闭）。", thread_id=tid)
            return
        try:
            self.mark_tg_activity(sid)
            self.store.last_sent_text[bid] = text
            # Snapshot before sending so diff only captures new output
            try:
                self.store.last_captures[bid] = self.rex.capture(sid, bid)
            except Exception:
                pass
            self.rex.send_text(sid, bid, text, press_enter=True)
            self.tg.react(chat_id, msg["message_id"], "⚡")
        except Exception as exc:
            self.tg.send_html(chat_id, f"❌ 发送到终端失败：<code>{html.escape(str(exc))}</code>", thread_id=tid)

    def handle_callback(self, cb: dict[str, Any]) -> None:
        cb_id = cb.get("id")
        sender = cb.get("from") or {}
        user_id = sender.get("id")
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        msg_id = msg.get("message_id")
        data = cb.get("data") or ""

        with self.store.lock:
            bound_chat = self.store.data.get("chat_id")
            allowed_user = self.store.data.get("allowed_user_id")
        if not self.is_bound() or user_id != allowed_user or chat_id != bound_chat:
            if cb_id:
                try:
                    self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "未授权"})
                except Exception:
                    pass
            return

        sid, tid = self._resolve_topic_session(msg)

        # Global mode callbacks work in General or any Topic
        if data.startswith("mode:"):
            action = data.split(":", 1)[1]
            with self.store.lock:
                if action in ("smart", "away", "always"):
                    self.store.data["notify_mode"] = action
                elif action == "urgent":
                    self.store.data["always_push_urgent"] = not self.get_always_push_urgent()
                self.store.save()
            try:
                self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "已更新推送策略"})
                status_html, kb = self._mode_status_html_and_kb(sid)
                self.tg.call(
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": status_html,
                        "parse_mode": "HTML",
                        "reply_markup": kb,
                    },
                )
            except Exception as exc:
                log.warning("mode callback error: %s", exc)
            return

        if not sid:
            with self.store.lock:
                route = self.store.data.get("msg_routes", {}).get(str(msg_id))
                if route:
                    sid = route.get("session_id")
                    tid = (self.store.data.get("sessions", {}).get(sid) or {}).get("topic_id")

        if not sid:
            self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "未找到对应 Session"})
            return

        try:
            if data == "kill_no":
                self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "已取消"})
                return
            if data == "kill_ok":
                self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "正在关闭 Session 并删除话题..."})
                try:
                    self.rex.kill_session(sid)
                except Exception as exc:
                    log.warning("kill_session(%s): %s", sid, exc)
                self.delete_topic_and_cleanup(sid)
                return
            if data.startswith("use:"):
                suffix = data.split(":", 1)[1]
                _, blks = self.rex.list_session_blocks(sid)
                matched = next((b for b in blks if b["block_id"].endswith(suffix)), None)
                if not matched:
                    self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Pane 不存在"})
                    return
                with self.store.lock:
                    self.store.data["sessions"][sid]["active_block_id"] = matched["block_id"]
                    self.store.save()
                self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"已切换到 {matched['program']}"})
                self.tg.send_html(
                    chat_id,
                    f"🎯 已切换输入目标到 <b>{html.escape(matched['program'])}</b> (<code>{suffix}</code>)",
                    thread_id=tid,
                )
                return

            bid = self._resolve_target(sid, msg_id)
            if not bid:
                self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Pane 已关闭"})
                return

            if data == "scr":
                self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "正在截屏..."})
                full_cap = self.rex.capture(sid, bid)
                self.store.last_captures[bid] = full_cap
                cleaned = clean_tui_output(full_cap)
                content = "\n".join(cleaned.splitlines()[-CAPTURE_LINES:])
                self.push_to_topic(sid, bid, "📸 <b>终端内容</b>", content or "(空白屏幕)", kb_kind="screen", as_code=True, render_md=False)
                return

            if data.startswith("k:"):
                self.mark_tg_activity(sid)
                key = data.split(":", 1)[1]
                if key in ("y", "n", "1", "2", "3", "4"):
                    self.store.last_sent_text[bid] = key
                    self.rex.send_text(sid, bid, key, press_enter=True)
                else:
                    self.rex.send_keys(sid, bid, [key])
                self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"已发送 {key}"})
                return
        except Exception as exc:
            log.warning("Callback error: %s", exc)
            try:
                self.tg.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"失败: {str(exc)[:60]}"})
            except Exception:
                pass

    def run_telegram_loop(self) -> None:
        me = self.tg.call("getMe", timeout=20)
        self.bot_username = me.get("username", "")
        log.info("Telegram bot connected: @%s (id=%s)", self.bot_username, me.get("id"))

        try:
            self.tg.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "mode", "description": "查看/切换防打扰模式 (smart/away/always)"},
                        {"command": "mute", "description": "静音当前话题"},
                        {"command": "watch", "description": "强制关注当前话题"},
                        {"command": "unmute", "description": "恢复当前话题跟随全局模式"},
                        {"command": "sync", "description": "同步全部 Rex Sessions 为 Topics"},
                        {"command": "sessions", "description": "列出在线 Rex Sessions"},
                        {"command": "new", "description": "新建 Rex Session (例: /new task agy)"},
                        {"command": "screen", "description": "查看终端内容（自动去噪）"},
                        {"command": "rawscreen", "description": "查看原始终端截屏"},
                        {"command": "panes", "description": "查看/切换当前 Session 的分屏"},
                        {"command": "key", "description": "发送按键 (如 ctrl+c, esc)"},
                        {"command": "raw", "description": "输入文本但不回车"},
                        {"command": "rename", "description": "重命名 Session 与话题"},
                        {"command": "kill", "description": "关闭当前 Session"},
                        {"command": "bind", "description": "绑定当前 Topics 群与管理员"},
                        {"command": "help", "description": "查看命令帮助"},
                    ]
                },
                timeout=15,
            )
        except Exception as exc:
            log.warning("setMyCommands failed: %s", exc)

        if self.is_bound():
            try:
                active, closed = self.sync_all_sessions()
                log.info("Initial session sync complete: active=%d closed=%d", active, closed)
            except Exception as exc:
                log.warning("Initial sync failed: %s", exc)

        offset = 0
        while not self.stop_event.is_set():
            try:
                updates = self.tg.call(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 30,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=45,
                )
                for upd in updates or []:
                    offset = max(offset, int(upd["update_id"]) + 1)
                    if "message" in upd:
                        threading.Thread(
                            target=self.handle_telegram_message,
                            args=(upd["message"],),
                            daemon=True,
                        ).start()
                    elif "callback_query" in upd:
                        threading.Thread(
                            target=self.handle_callback,
                            args=(upd["callback_query"],),
                            daemon=True,
                        ).start()
            except Exception as exc:
                log.warning("getUpdates error: %s", exc)
                time.sleep(3)


def make_hook_server(bridge: Bridge) -> socketserver.ThreadingTCPServer:
    class HookHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("hook http: " + fmt, *args)

        def _send_json(self, code: int, obj: dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "bound": bridge.is_bound(),
                        "bot": bridge.bot_username,
                        "sessions": len(bridge.store.data.get("sessions", {})),
                    },
                )
            else:
                self._send_json(404, {"ok": False})

        def do_POST(self) -> None:
            if self.path != "/hook":
                self._send_json(404, {"ok": False})
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
                res = bridge.handle_hook_payload(data)
                self._send_json(200 if res.get("ok") else 400, res)
            except Exception as exc:
                log.warning("Hook POST error: %s", exc)
                self._send_json(500, {"ok": False, "error": str(exc)})

    class ReusableThreadingHTTPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    return ReusableThreadingHTTPServer((HOOK_HOST, HOOK_PORT), HookHandler)


def main() -> int:
    if not TG_BOT_TOKEN:
        log.error("TG_BOT_TOKEN is not set. Set TG_BOT_TOKEN or pass --env <file>.")
        return 2

    store = StateStore(STATE_FILE)
    tg = TelegramAPI(TG_BOT_TOKEN, TG_PROXY, TG_API_BASE)
    rex = RexClient(REX_BIN)
    bridge = Bridge(tg, rex, store)

    hook_srv = make_hook_server(bridge)
    threading.Thread(target=hook_srv.serve_forever, name="hook-http", daemon=True).start()
    log.info("Hook endpoint listening on http://%s:%d/hook", HOOK_HOST, HOOK_PORT)

    threading.Thread(target=bridge.run_rex_events_loop, name="rex-events", daemon=True).start()

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info("Received signal %s, exiting...", signum)
        bridge.stop_event.set()
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    bridge.run_telegram_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
