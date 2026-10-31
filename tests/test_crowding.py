#!/usr/bin/env python3
"""Offline checks for crowding.py: COT index math and the point-in-time rule.
Run: python tests/test_crowding.py"""
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crowding as C  # noqa: E402

pd, np = C.pd, C.np


def main():
    # COT index: 100 at the window max, 0 at the window min, 3-year window.
    n = 200
    net = np.concatenate([np.linspace(0, 1000, 100), np.linspace(1000, 500, 100)])
    df = pd.DataFrame({"L": net + 5000, "S": np.full(n, 5000.0), "open_interest_all": 20000.0})
    m = C.group_metrics(df, "L", "S")
    assert m["cot_index"].iloc[99] == 100.0
    assert np.isnan(m["cot_index"].iloc[10])            # under 52 reports of history
    assert m["net_pct_oi"].iloc[99] == 5.0
    # At the end the window (last 156) spans 1000 down to 500 and part of the climb.
    w = net[-156:]
    exp = round((net[-1] - w.min()) / (w.max() - w.min()) * 100, 1)
    assert m["cot_index"].iloc[-1] == exp, (m["cot_index"].iloc[-1], exp)

    # Point-in-time: Tuesday's positions are usable only once released.
    tmp = Path(tempfile.mkdtemp())
    C.COT = tmp
    pd.DataFrame({
        "report_date": ["2026-10-13", "2026-10-20", "2026-10-27"],
        "release_nominal_et": ["2026-10-16 15:30", "2026-10-23 15:30", "2026-10-30 15:30"],
        "first_seen_utc": ["2026-10-28T18:47:09+00:00", "2026-10-28T18:47:09+00:00",
                           "2026-10-31T01:57:00+00:00"],
        "backfilled": ["True", "True", "False"],
    }).to_csv(tmp / "releases.csv", index=False)
    rel = C.release_times()
    assert rel["2026-10-20"] == pd.Timestamp("2026-10-23 19:30", tz="UTC")       # nominal
    assert rel["2026-10-27"] == pd.Timestamp("2026-10-31 01:57", tz="UTC")       # first seen live

    hist = pd.DataFrame({"symbol": "ES", "report_date": rel.index,
                         "available_at": rel.values, "x": [1, 2, 3]})
    def at(s):
        return C.as_of(hist, datetime.fromisoformat(s))["report_date"].item()
    assert at("2026-10-22T20:00:00+00:00") == "2026-10-13"   # Thu: Tue's report not out
    assert at("2026-10-23T19:29:00+00:00") == "2026-10-13"   # one minute before release
    assert at("2026-10-23T20:00:00+00:00") == "2026-10-20"   # Fri after 3:30 PM ET
    assert at("2026-10-30T21:00:00+00:00") == "2026-10-20"   # live report not yet seen
    assert at("2026-10-31T02:00:00+00:00") == "2026-10-27"
    print("all checks passed")


if __name__ == "__main__":
    main()
