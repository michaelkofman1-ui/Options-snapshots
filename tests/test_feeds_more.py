#!/usr/bin/env python3
"""Offline checks for the bars and events feeds. Run: python tests/test_feeds_more.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feeds as F  # noqa: E402
import feeds_more as M  # noqa: E402

pd = M.pd

FOMC_HTML = """
<h4><a id="1">2027 FOMC Meetings</a></h4>
<div class="fomc-meeting__month"><strong>January</strong></div><div class="fomc-meeting__date">26-27</div>
<div class="fomc-meeting__month"><strong>March</strong></div><div class="fomc-meeting__date">16-17*</div>
<h4><a id="2">2026 FOMC Meetings</a></h4>
<div><strong>October</strong></div><div>27-28</div>
<p>Minutes: (Released November 18, 2026)</p>
<div><strong>December</strong></div><div>8-9*</div>
<h4><a id="3">2025 FOMC Meetings</a></h4>
<div><strong>Apr/May</strong></div><div>30-1</div>
<div><strong>August</strong></div><div>14 (unscheduled)</div>
"""


def main():
    f = M.parse_fomc(FOMC_HTML)
    assert f["date"].tolist() == ["2027-01-27", "2027-03-17", "2026-10-28", "2026-12-09",
                                  "2025-05-01", "2025-08-14"], f["date"].tolist()
    assert "projections" in f.set_index("date").loc["2026-12-09", "detail"]
    assert f.set_index("date").loc["2025-08-14", "detail"].startswith("unscheduled")

    # yfinance-shaped download -> tidy bars
    idx = pd.DatetimeIndex(["2026-10-28", "2026-10-29"], name="Date")
    cols = pd.MultiIndex.from_product([["QQQ"], ["Open", "High", "Low", "Close", "Adj Close",
                                                 "Volume", "Dividends", "Stock Splits"]])
    raw = pd.DataFrame([[1, 2, 0.5, 1.5, 1.4, 100, 0, 0], [1.5, 2, 1, 1.2, 1.1, 120, 0, 0]],
                       index=idx, columns=cols)
    b = M._frame(raw, "QQQ", True)
    assert list(b.columns) == ["date", "open", "high", "low", "close", "volume", "dividends", "splits"]
    assert b["date"].tolist() == ["2026-10-28", "2026-10-29"]

    tmp = Path(tempfile.mkdtemp())
    # Bar history guard: recent bars may settle, older closes may not change.
    p = tmp / "QQQ.csv"
    hist = pd.DataFrame({"date": [f"2026-10-{d:02d}" for d in range(1, 21)], "close": [float(x) for x in range(100, 120)]})
    hist.to_csv(p, index=False)
    ok = hist.copy()
    ok.loc[19, "close"] = 119.5           # last bar settles: fine
    F.check_bar_history(p, ok)
    bad = hist.copy()
    bad.loc[2, "close"] = 90              # old close rewritten: refused
    try:
        F.check_bar_history(p, bad)
        raise AssertionError("guard did not fire")
    except F.Refused:
        pass

    # Calendar merge: past rows kept, future rows follow the source.
    c = tmp / "macro.csv"
    first = pd.DataFrame({"date": ["2026-10-10", "2026-11-12"], "event": ["CPI", "CPI"], "time_et": "08:30"})
    M.merge_calendar(c, first, ["date", "event"], "2026-10-30")
    moved = pd.DataFrame({"date": ["2026-11-13"], "event": ["CPI"], "time_et": "08:30"})
    st, msg = M.merge_calendar(c, moved, ["date", "event"], "2026-10-30")
    got = pd.read_csv(c)["date"].tolist()
    assert got == ["2026-10-10", "2026-11-13"], got   # history kept, old future date gone

    # Combined calendar
    (tmp / "fomc.csv").write_text("date,time_et,event,detail\n2026-10-28,14:00,FOMC,scheduled\n")
    (tmp / "earnings.csv").write_text(
        "symbol,date,time_et,session,eps_estimate,eps_reported,surprise_pct\n"
        "MSFT,2026-10-28,16:05,after close,3.1,3.4,9.7\nNVDA,2026-11-19,16:20,after close,1.2,,\n")
    ev = M.combine_events(tmp, "2026-10-30")
    assert ev["event"].tolist()[:3] == ["CPI", "FOMC", "Earnings"], ev
    assert ev[ev.symbol == "MSFT"]["detail"].item() == "after close, surprise +9.7%"
    assert ev[ev.symbol == "NVDA"]["scheduled"].item()
    print("all checks passed")


if __name__ == "__main__":
    main()
