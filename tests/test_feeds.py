#!/usr/bin/env python3
"""Offline checks for feeds.py: merge rules, source parsing, COT release dates.
Run: python tests/test_feeds.py   (no network, writes only to a temp dir)"""
import json
import sys
import tempfile
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feeds as F  # noqa: E402

pd = F.pd


def expect_refused(fn):
    try:
        fn()
    except F.Refused:
        return
    raise AssertionError("expected Refused")


def main():
    tmp = Path(tempfile.mkdtemp())
    F.EXT = tmp
    p = tmp / "s.csv"
    base = pd.DataFrame({"date": [f"2020-01-{d:02d}" for d in range(1, 11)],
                         "value": [float(v) for v in range(10)]})

    assert F.merge_store(p, base.copy(), ["date"])[0] == "saved"
    assert F.merge_store(p, base.copy(), ["date"])[0] == "same"

    # Appending a day saves; held rows stay.
    more = pd.concat([base, pd.DataFrame({"date": ["2020-01-11"], "value": [10.0]})])
    st, msg = F.merge_store(p, more.copy(), ["date"])
    assert st == "saved" and "+1" in msg, msg

    # Source truncates its history: rows kept, status warn.
    st, msg = F.merge_store(p, more.iloc[5:].copy(), ["date"])
    assert st == "warn" and len(pd.read_csv(p)) == 11, msg

    # Partial window (COT refresh) is not a truncation.
    st, msg = F.merge_store(p, more.iloc[8:].copy(), ["date"], partial=True)
    assert st == "same", (st, msg)

    # Garbage that rewrites history is refused, file untouched.
    bad = more.copy()
    bad["value"] = 0.0
    expect_refused(lambda: F.merge_store(p, bad.copy(), ["date"]))
    assert pd.read_csv(p)["value"].iloc[-1] == 10.0
    # ...unless the series is known to revise its history.
    assert F.merge_store(p, bad.copy(), ["date"], revises=True)[0] == "saved"
    expect_refused(lambda: F.merge_store(p, base.iloc[:0].copy(), ["date"]))

    # Cboe parsing: VIX has OHLC, VVIX has one value column.
    F.http_get = lambda url, **k: (
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17.24,17.24,17.24,17.24\n01/03/1990,18.19,18.19,18.19,18.19\n"
        if "/VIX_History" in url else b"DATE,VVIX\n03/06/2006,71.73\n03/07/2006,70.10\n")
    v = F.fetch_vol("VIX")
    assert list(v.columns) == ["date", "open", "high", "low", "close"] and v["date"].iloc[0] == "1990-01-02"
    vv = F.fetch_vol("VVIX")
    assert list(vv.columns) == ["date", "close"] and vv["close"].iloc[1] == 70.10

    # FRED parsing: "." means missing.
    F.http_get = lambda url, **k: b"observation_date,BAA10Y\n1986-01-02,1.90\n1986-01-03,.\n1986-01-06,1.95\n"
    fr = F.fetch_fred("BAA10Y")
    assert len(fr) == 2 and fr["value"].tolist() == [1.90, 1.95]

    # COT: paging, parsing, year files, markets and release dates.
    def fake_cot(url, **k):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if int(q["$offset"][0]) > 0:
            return b"[]"
        start = q["$where"][0].split("'")[1][:10]
        if not start.startswith("2026"):
            return b"[]"
        rows = []
        for d in ["2026-10-13", "2026-10-20"]:
            for code, name in [("13874A", "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE"),
                               ("209742", "NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE")]:
                rows.append({"report_date_as_yyyy_mm_dd": f"{d}T00:00:00.000",
                             "cftc_contract_market_code": code,
                             "market_and_exchange_names": name,
                             "commodity_name": "STOCK INDEX", "commodity_group_name": "FINANCIAL INSTRUMENTS",
                             "commodity_subgroup_name": "STOCK INDICES", "contract_units": "x",
                             "open_interest_all": "1000", "lev_money_positions_long": "100",
                             "lev_money_positions_short": "300"})
        return json.dumps(rows).encode()

    F.http_get = fake_cot
    F.COT_FIRST_YEAR = 2025
    now = datetime(2026, 10, 28, 18, tzinfo=timezone.utc)
    rec = F.Recorder()
    F.run_cot("tff", now, rec)
    res = rec.rows
    assert ("cot/tff/2026", "saved") in [(r[0], r[1]) for r in res], res
    y = pd.read_csv(tmp / "cot" / "tff" / "2026.csv", dtype={"code": str})
    assert len(y) == 4 and y["lev_money_positions_short"].iloc[0] == 300
    m = pd.read_csv(tmp / "cot" / "markets.csv", dtype=str)
    assert set(m["code"]) == {"13874A", "209742"}
    rel = pd.read_csv(tmp / "cot" / "releases.csv", dtype=str)
    assert rel["release_nominal_et"].tolist() == ["2026-10-16 15:30", "2026-10-23 15:30"]
    assert rel["backfilled"].tolist() == ["True", "True"]
    # Second run only refreshes the window and changes nothing.
    rec2 = F.Recorder()
    F.run_cot("tff", now, rec2)
    assert all(r[1] == "same" for r in rec2.rows if r[0].endswith("2026")), rec2.rows
    assert (tmp / "_last_run.csv").exists()

    assert F.cot_release_nominal(date(2026, 10, 20)).strftime("%a %H:%M") == "Fri 15:30"
    print("all checks passed")


if __name__ == "__main__":
    main()
