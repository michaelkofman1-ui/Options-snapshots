#!/usr/bin/env python3
"""Offline checks for snapshot.py on a synthetic Black-Scholes chain.
Run: python tests/test_snapshot.py   (no network, writes only to a temp dir)"""
import math
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import snapshot as S  # noqa: E402


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(spot, k, t, vol, cp):
    if t <= 0:
        intrinsic = max(spot - k, 0) if cp == "C" else max(k - spot, 0)
        return intrinsic, (1.0 if spot > k else 0.0) if cp == "C" else (-1.0 if spot < k else 0.0)
    d1 = (math.log(spot / k) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if cp == "C":
        return spot * ncdf(d1) - k * ncdf(d2), ncdf(d1)
    return k * ncdf(-d2) - spot * ncdf(-d1), ncdf(d1) - 1


def smile(k, spot, base=0.20, slope=-0.4):
    # Puts (low strikes) richer than calls, like an equity index.
    return max(0.05, base + slope * math.log(k / spot))


def fake_payload(trade_day: date, stamp_utc: str, spot=600.0, n_strikes=81, skew=-0.4,
                 last_trade=None):
    opts = []
    for dte in (0, 3, 10, 24, 38, 52, 80, 115, 170, 400):
        exp = trade_day + timedelta(days=dte)
        t = max(dte, 0) / 365.0
        for i in range(n_strikes):
            k = round(spot * (0.6 + 0.8 * i / (n_strikes - 1)))
            for cp in "CP":
                vol = smile(k, spot, slope=skew)
                px, delta = bs(spot, k, t, vol, cp)
                opts.append({
                    "option": f"SPY{exp:%y%m%d}{cp}{int(k * 1000):08d}",
                    "bid": round(max(px - 0.02, 0), 2), "ask": round(px + 0.02, 2),
                    "bid_size": 10.0, "ask_size": 10.0, "iv": vol, "delta": delta,
                    "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0, "theo": px,
                    "open_interest": 100.0, "volume": 50.0 if dte == 0 else 10.0,
                    "last_trade_time": last_trade, "prev_day_close": px,
                })
    return {"timestamp": stamp_utc, "data": {"options": opts, "current_price": spot, "iv30": 0.2}}


def main():
    tmp = Path(tempfile.mkdtemp())
    S.CHAINS, S.SUMMARY = tmp / "chains", tmp / "summary.csv"
    day = date(2026, 10, 28)
    now = datetime.now(timezone.utc)

    # Session classification (feed stamps are UTC; NY is UTC-4 on this date).
    from datetime import datetime as DT
    def cls(stamp, lt=None):
        s, _, d = S.classify(S.feed_time({"timestamp": stamp}), DT.fromisoformat(lt) if lt else None)
        return s, d.isoformat()
    assert cls("2026-10-28 16:10:17") == ("intraday", "2026-10-28")
    assert cls("2026-10-28 20:30:00") == ("close", "2026-10-28")           # 4:30 PM NY
    assert cls("2026-10-29 00:05:31") == ("close", "2026-10-28")           # 8:05 PM NY
    # A run delayed past midnight still files the close it sees...
    assert cls("2026-10-29 04:20:00", "2026-10-28T16:14:59") == ("close", "2026-10-28")
    assert cls("2026-10-28 11:00:00") == ("close", "2026-10-27")           # 7 AM NY
    assert cls("2026-10-31 15:00:00", "2026-10-30T16:14:00") == ("close", "2026-10-30")  # Saturday
    # ...unless overnight trading (SPX/NDX/VIX) has moved the quotes.
    assert cls("2026-10-29 01:06:00", "2026-10-28T20:59:00")[0] is None
    assert cls("2026-10-29 04:20:00", "2026-10-29T00:15:00")[0] is None
    # Holiday: stamp on a weekday with no trades that day -> previous close.
    assert cls("2026-11-26 22:00:00", "2026-11-25T16:14:00") == ("close", "2026-11-25")

    summ = S.load_summary()

    # 1) intraday snapshot saves
    st, msg, summ = S.process("SPY", fake_payload(day, "2026-10-28 16:10:17"), summ, now)
    assert st == "saved", msg
    # 2) same stamp again is a no-op
    st, msg, summ = S.process("SPY", fake_payload(day, "2026-10-28 16:10:17"), summ, now)
    assert st == "same", msg
    # 3) post-close replaces intraday
    st, msg, summ = S.process("SPY", fake_payload(day, "2026-10-28 20:31:00"), summ, now)
    assert st == "saved", msg
    # 4) a later intraday stamp never replaces a close
    st, msg, summ = S.process("SPY", fake_payload(day, "2026-10-28 19:59:00"), summ, now)
    assert st == "same", msg
    # 5) the first close is kept; a later close is a no-op
    st, msg, summ = S.process("SPY", fake_payload(day, "2026-10-28 23:00:00"), summ, now)
    assert st == "same", msg
    # 6) a much smaller chain is refused (later intraday over intraday)
    _, _, summ = S.process("QQQ", fake_payload(day, "2026-10-28 15:00:00"), summ, now)
    try:
        S.process("QQQ", fake_payload(day, "2026-10-28 16:00:00", n_strikes=21), summ, now)
        raise AssertionError("shrink guard did not fire")
    except ValueError as e:
        assert "shrink" in str(e)
    # 7) overnight-session quotes are skipped
    st, msg, summ = S.process("SPX", fake_payload(day, "2026-10-29 01:06:00",
                                                  last_trade="2026-10-28T21:00:00"), summ, now)
    assert st == "skip", msg

    row = summ[summ.symbol == "SPY"].iloc[0]
    df = S.pd.read_parquet(S.chain_path("SPY", "2026-10-28"))
    print(f"kept {len(df)} of {row.n_raw} contracts, {row.n_expirations} expirations, "
          f"file {S.chain_path('SPY', '2026-10-28').stat().st_size / 1024:.0f} KB")
    print({k: round(float(row[k]), 4) for k in
           ["spot", "spot_parity", "atm_iv_30", "put25_iv_30", "call25_iv_30", "skew25_30",
            "put16_iv_45", "call16_iv_45", "zero_dte_volume_share"]})

    assert df["dte"].max() <= S.DTE_MAX and df["delta"].abs().between(0.01, 0.65).all()
    assert abs(row.spot_parity - 600) < 1.0, row.spot_parity
    assert abs(row.atm_iv_30 - 0.20) < 0.01, row.atm_iv_30
    assert row.put25_iv_30 > row.atm_iv_30 > row.call25_iv_30   # put skew recovered
    assert row.skew25_30 > 0.02
    assert row.session == "close" and row.feed_ts_ny == "2026-10-28 16:31:00"
    print("all checks passed")


if __name__ == "__main__":
    main()
