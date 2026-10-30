#!/usr/bin/env python3
"""Offline checks for notify.py. Run: python tests/test_notify.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notify as N  # noqa: E402

tmp = Path(tempfile.mkdtemp())
N.CONFIG = tmp / "telegram.json"
sent = []


def fake_call(method, **p):
    if method == "getMe":
        return {"username": "omhf_alerts_bot"}
    if method == "getUpdates":
        return [{"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "M"},
                                             "text": "/start"}}]
    if method == "sendMessage":
        sent.append(p)
        return {}
    raise AssertionError(method)


# No token: silent no-op, never raises.
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
assert N.send("x") is False

os.environ["TELEGRAM_BOT_TOKEN"] = "test"
N.call = fake_call
N.setup()
assert json.loads(N.CONFIG.read_text())["chat_id"] == 42 and sent[-1]["chat_id"] == 42

csvp = tmp / "run.csv"
csvp.write_text("feed,status,detail\nvol/VIX,same,ok\nfred/BAA10Y,FAIL,timed out\n")
assert N.fail_lines(str(csvp)) == ["• fred/BAA10Y: timed out"]
assert N.send("hello") and sent[-1]["text"] == "hello"
print("all checks passed")
