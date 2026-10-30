#!/usr/bin/env python3
"""
Telegram notifications for the data jobs (and later the fund alerter).

  python notify.py --setup            find your chat with the bot, save its id
                                      to config/telegram.json, send a test message
  python notify.py "text"             send a message
  python notify.py "text" --fails data/external/_last_run.csv
                                      append the FAIL rows of a status CSV

Needs the TELEGRAM_BOT_TOKEN secret. The token is never printed. Without
it, sending is a silent no-op, so a missing secret can't break a job.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config" / "telegram.json"
API = "https://api.telegram.org/bot{token}/{method}"


def call(method: str, **params) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:  # message only; the URL holds the token
        try:
            desc = json.load(e).get("description", "")
        except Exception:
            desc = ""
        raise RuntimeError(f"Telegram {method} failed: HTTP {e.code} {desc}".strip()) from None
    except Exception as e:
        raise RuntimeError(f"Telegram {method} failed: {type(e).__name__}") from None
    if not out.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {out.get('description')}")
    return out["result"]


def chat_id() -> str | None:
    env = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env:
        return env
    if CONFIG.exists():
        return str(json.loads(CONFIG.read_text()).get("chat_id") or "") or None
    return None


def send(text: str) -> bool:
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        print("No TELEGRAM_BOT_TOKEN; skipping notification.")
        return False
    cid = chat_id()
    if not cid:
        print("No chat id yet; run the telegram-setup workflow.")
        return False
    call("sendMessage", chat_id=cid, text=text[:4000], disable_web_page_preview="true")
    return True


def setup() -> None:
    me = call("getMe")
    updates = call("getUpdates")
    chats = [u["message"]["chat"] for u in updates
             if "message" in u and u["message"].get("chat", {}).get("type") == "private"]
    if not chats:
        raise SystemExit(f"No messages to @{me.get('username')} yet. Open the bot in "
                         "Telegram, tap Start, then run this again.")
    chat = chats[-1]
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"bot": me.get("username"), "chat_id": chat["id"]}, indent=2) + "\n")
    call("sendMessage", chat_id=chat["id"], text=(
        "✅ OMHF alerts connected.\n\n"
        "For now this chat gets a message when a data job fails (option snapshots, "
        "vol indices, COT, FRED). Trade alerts will come here too once the alerter is built."))
    print(f"Connected @{me.get('username')} to chat of {chat.get('first_name', 'you')}.")


def fail_lines(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open() as f:
        rows = [r for r in csv.DictReader(f) if r.get("status") == "FAIL"]
    name_key = "feed" if rows and "feed" in rows[0] else "symbol"
    return [f"• {r.get(name_key)}: {r.get('detail', '')[:120]}" for r in rows[:15]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--fails", help="status CSV whose FAIL rows to append")
    args = ap.parse_args()
    if args.setup:
        # Outcome goes to a committed file too, since the Actions logs aren't
        # always at hand. Messages never contain the token.
        status = CONFIG.parent / "telegram_setup_status.txt"
        status.parent.mkdir(parents=True, exist_ok=True)
        try:
            setup()
            status.write_text("ok\n")
            return 0
        except (RuntimeError, SystemExit) as e:
            status.write_text(f"failed: {e}\n")
            print(e)
            return 1
    text = args.text or "(no text)"
    if args.fails:
        lines = fail_lines(args.fails)
        if lines:
            text += "\n\n" + "\n".join(lines)
    try:
        send(text)
    except RuntimeError as e:  # never fail the job over a notification
        print(e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
