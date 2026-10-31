#!/usr/bin/env python3
"""
Crowding monitor: weekly COT positioning extremes across the core futures.

Reads the CFTC data that feeds.py keeps in data/external/cot/ and writes:

  data/derived/crowding/history.csv   every market x every report: net, COT
                                      index, z-score, net % OI, weekly change,
                                      and when the report became public
  data/derived/crowding/latest.csv    the latest report, ranked by extremity,
                                      with price context

Then sends a Telegram summary of the crowded markets once per new report.

Definitions (per market, per trader group):
  net        = long - short
  COT index  = (net - min) / (max - min) * 100 over the trailing 156 reports
               (3 years), using only reports up to and including that one
  z          = (net - mean) / std over the same window
  crowded    = primary group's COT index >= 90 (long) or <= 10 (short)
Primary group: leveraged funds (TFF) for financials, managed money
(disaggregated) for commodities. Legacy large speculators are shown for all.

Lesson from the QQQ/IWM study: crowding alone was weak. This is context;
a price failure against the crowd is the trigger.

Point-in-time: a report is usable only from `available_at` (when we first saw
it live, or the nominal Friday 3:30 PM ET release for backfilled history).
`as_of()` enforces that and is unit-tested.

  python crowding.py              compute, write, send if the report is new
  python crowding.py --no-send    compute and write only
  python crowding.py --force-send send even if this report was already sent
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
COT = ROOT / "data" / "external" / "cot"
OUT = ROOT / "data" / "derived" / "crowding"
NY = ZoneInfo("America/New_York")
LOOKBACK = 156
MIN_HISTORY = 52       # fewer reports than this -> index not reported
CROWDED_HI, CROWDED_LO = 90, 10

# symbol, name, sector, CFTC code, Yahoo symbol for price context
UNIVERSE = [
    ("ES", "S&P 500 E-mini", "equity", "13874A", "ES=F"),
    ("NQ", "Nasdaq-100 E-mini", "equity", "209742", "NQ=F"),
    ("RTY", "Russell 2000 E-mini", "equity", "239742", "RTY=F"),
    ("YM", "Dow E-mini", "equity", "124603", "YM=F"),
    ("VX", "VIX futures", "equity", "1170E1", "^VIX"),
    ("ZT", "2-year note", "rates", "042601", "ZT=F"),
    ("ZF", "5-year note", "rates", "044601", "ZF=F"),
    ("ZN", "10-year note", "rates", "043602", "ZN=F"),
    ("TN", "Ultra 10-year", "rates", "043607", "TN=F"),
    ("ZB", "30-year bond", "rates", "020601", "ZB=F"),
    ("UB", "Ultra bond", "rates", "020604", "UB=F"),
    ("6E", "Euro", "fx", "099741", "6E=F"),
    ("6J", "Japanese yen", "fx", "097741", "6J=F"),
    ("6B", "British pound", "fx", "096742", "6B=F"),
    ("6A", "Australian dollar", "fx", "232741", "6A=F"),
    ("6C", "Canadian dollar", "fx", "090741", "6C=F"),
    ("6S", "Swiss franc", "fx", "092741", "6S=F"),
    ("6M", "Mexican peso", "fx", "095741", "6M=F"),
    ("DX", "US dollar index", "fx", "098662", "DX=F"),
    ("BTC", "Bitcoin", "crypto", "133741", "BTC=F"),
    ("ETH", "Ether", "crypto", "146021", "ETH=F"),
    ("CL", "WTI crude", "energy", "067651", "CL=F"),
    ("NG", "Natural gas", "energy", "023651", "NG=F"),
    ("RB", "RBOB gasoline", "energy", "111659", "RB=F"),
    ("HO", "Heating oil (ULSD)", "energy", "022651", "HO=F"),
    ("GC", "Gold", "metals", "088691", "GC=F"),
    ("SI", "Silver", "metals", "084691", "SI=F"),
    ("HG", "Copper", "metals", "085692", "HG=F"),
    ("PL", "Platinum", "metals", "076651", "PL=F"),
    ("ZC", "Corn", "ags", "002602", "ZC=F"),
    ("ZS", "Soybeans", "ags", "005602", "ZS=F"),
    ("ZW", "Chicago wheat", "ags", "001602", "ZW=F"),
    ("KE", "KC wheat", "ags", "001612", "KE=F"),
    ("ZL", "Soybean oil", "ags", "007601", "ZL=F"),
    ("ZM", "Soybean meal", "ags", "026603", "ZM=F"),
    ("KC", "Coffee", "softs", "083731", "KC=F"),
    ("SB", "Sugar", "softs", "080732", "SB=F"),
    ("CC", "Cocoa", "softs", "073732", "CC=F"),
    ("CT", "Cotton", "softs", "033661", "CT=F"),
    ("LE", "Live cattle", "livestock", "057642", "LE=F"),
    ("HE", "Lean hogs", "livestock", "054642", "HE=F"),
]
FINANCIAL = {"equity", "rates", "fx", "crypto"}

# group -> (report, long column, short column)
GROUPS = {
    "spec": ("legacy", "noncomm_positions_long_all", "noncomm_positions_short_all"),
    "levfund": ("tff", "lev_money_positions_long", "lev_money_positions_short"),
    "assetmgr": ("tff", "asset_mgr_positions_long", "asset_mgr_positions_short"),
    "mm": ("disagg", "m_money_positions_long_all", "m_money_positions_short_all"),
}


# ── load ───────────────────────────────────────────────────────────────────
def load_report(report: str, codes: set[str]) -> pd.DataFrame:
    frames = []
    for f in sorted((COT / report).glob("*.csv")):
        df = pd.read_csv(f, dtype={"code": str})
        frames.append(df[df["code"].isin(codes)])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def release_times() -> pd.Series:
    """report_date -> available_at (UTC). Live reports: when we first saw them.
    Backfilled: the nominal Friday 3:30 PM ET release."""
    r = pd.read_csv(COT / "releases.csv", dtype=str)
    nominal = pd.to_datetime(r["release_nominal_et"]).dt.tz_localize(NY).dt.tz_convert("UTC")
    seen = pd.to_datetime(r["first_seen_utc"], utc=True)
    live = r["backfilled"].str.lower() != "true"
    avail = nominal.copy()
    avail[live] = pd.concat([nominal[live], seen[live]], axis=1).max(axis=1)
    avail.index = r["report_date"]
    return avail


# ── math ───────────────────────────────────────────────────────────────────
def group_metrics(df: pd.DataFrame, long_col: str, short_col: str) -> pd.DataFrame:
    """df: one market's rows for one report, sorted by report_date."""
    net = df[long_col] - df[short_col]
    oi = df["open_interest_all"]
    roll = net.rolling(LOOKBACK, min_periods=MIN_HISTORY)
    lo, hi = roll.min(), roll.max()
    rng = (hi - lo).replace(0, np.nan)
    out = pd.DataFrame({
        "net": net,
        "cot_index": ((net - lo) / rng * 100).round(1),
        "z": ((net - roll.mean()) / roll.std().replace(0, np.nan)).round(2),
        "net_pct_oi": (net / oi.replace(0, np.nan) * 100).round(1),
        "wow_change": net.diff(),
        "n_history": net.expanding().count().clip(upper=LOOKBACK).astype(int),
    }, index=df.index)
    return out


def build_history() -> pd.DataFrame:
    codes = {u[3] for u in UNIVERSE}
    reports = {name: load_report(name, codes) for name in {g[0] for g in GROUPS.values()}}
    rows = []
    for sym, name, sector, code, _ in UNIVERSE:
        base = None
        for g, (report, lc, sc) in GROUPS.items():
            df = reports[report]
            df = df[df["code"] == code].sort_values("report_date").drop_duplicates("report_date")
            if df.empty or lc not in df:
                continue
            m = group_metrics(df.reset_index(drop=True), lc, sc)
            m.columns = [f"{g}_{c}" for c in m.columns]
            m["report_date"] = df["report_date"].values
            if g == "spec":
                m["open_interest"] = df["open_interest_all"].values
            base = m if base is None else base.merge(m, on="report_date", how="outer")
        if base is None:
            continue
        base.insert(0, "symbol", sym)
        base.insert(1, "sector", sector)
        rows.append(base)
    hist = pd.concat(rows, ignore_index=True)
    avail = release_times()
    hist["available_at"] = hist["report_date"].map(avail)
    hist["primary"] = np.where(hist["sector"].isin(FINANCIAL), "levfund", "mm")
    hist["primary_index"] = np.where(hist["primary"] == "levfund",
                                     hist.get("levfund_cot_index"), hist.get("mm_cot_index"))
    hist["crowded"] = np.select([hist["primary_index"] >= CROWDED_HI,
                                 hist["primary_index"] <= CROWDED_LO], ["long", "short"], "")
    return hist.sort_values(["report_date", "symbol"]).reset_index(drop=True)


def as_of(hist: pd.DataFrame, when: datetime) -> pd.DataFrame:
    """Latest row per symbol that was public at `when` (timezone-aware)."""
    ts = pd.Timestamp(when).tz_convert("UTC")
    ok = hist[pd.to_datetime(hist["available_at"], utc=True) <= ts]
    return ok.sort_values("report_date").groupby("symbol").tail(1).reset_index(drop=True)


# ── price context ──────────────────────────────────────────────────────────
def price_context(yahoo: list[str]) -> pd.DataFrame:
    """4-week range, distance from 20/50-day averages, ATR(14) from Yahoo's
    continuous front-month series. Roll gaps distort these slightly; Sierra
    data replaces this once the collector is running. Empty on failure."""
    try:
        import yfinance as yf
        raw = yf.download(yahoo, period="6mo", interval="1d", auto_adjust=False,
                          group_by="ticker", progress=False, threads=True)
    except Exception as e:
        print(f"price context unavailable: {type(e).__name__}")
        return pd.DataFrame()
    out = []
    for t in yahoo:
        try:
            d = raw[t].dropna(subset=["Close"])
        except Exception:
            continue
        if len(d) < 50:
            continue
        c, h, l = d["Close"], d["High"], d["Low"]
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        last = c.iloc[-1]
        hi20, lo20 = h.iloc[-20:].max(), l.iloc[-20:].min()
        out.append({
            "yahoo": t, "price_date": d.index[-1].strftime("%Y-%m-%d"), "close": last,
            "atr14": atr,
            "vs_sma20_atr": (last - c.iloc[-20:].mean()) / atr if atr else np.nan,
            "vs_sma50_atr": (last - c.iloc[-50:].mean()) / atr if atr else np.nan,
            "pos_in_4wk_range": (last - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else np.nan,
        })
    return pd.DataFrame(out)


# ── report ─────────────────────────────────────────────────────────────────
def latest_table(hist: pd.DataFrame, now: datetime) -> pd.DataFrame:
    cur = as_of(hist, now)
    meta = pd.DataFrame(UNIVERSE, columns=["symbol", "name", "sector", "code", "yahoo"])
    cur = cur.merge(meta[["symbol", "name", "yahoo"]], on="symbol", how="left")
    px = price_context(meta["yahoo"].tolist())
    if not px.empty:
        cur = cur.merge(px, on="yahoo", how="left")
        # Price turning against the crowd: the part that matters (it's the trigger).
        cur["against_crowd"] = np.where(
            (cur["crowded"] == "long") & (cur["vs_sma20_atr"] < 0), "price below 20-day",
            np.where((cur["crowded"] == "short") & (cur["vs_sma20_atr"] > 0), "price above 20-day", ""))
    cur["extremity"] = (cur["primary_index"] - 50).abs()
    return cur.sort_values("extremity", ascending=False).reset_index(drop=True)


def fmt_row(r: pd.Series) -> str:
    g = "lev funds" if r["primary"] == "levfund" else "money mgrs"
    wow = r.get(f"{r['primary']}_wow_change")
    wow_s = f", {'+' if wow > 0 else ''}{wow:,.0f} w/w" if pd.notna(wow) else ""
    spec = r.get("spec_cot_index")
    spec_s = f"; large specs {spec:.0f}" if pd.notna(spec) else ""
    px = ""
    if pd.notna(r.get("pos_in_4wk_range")):
        px = f"\n    price {r['pos_in_4wk_range']:.0f}% of 4-wk range, {r['vs_sma20_atr']:+.1f} ATR vs 20-day"
        if r.get("against_crowd"):
            px += f"  ⚠️ {r['against_crowd']}"
    via = ""
    if r.get("crowded") in ("long", "short"):
        try:
            import instruments
            fade = "short" if r["crowded"] == "long" else "long"
            cfg = fund_config()
            via = (f"\n    fade via: {instruments.expression(r['symbol'], fade, cfg.get('swing_book_usd'), cfg.get('risk_pct', 0.5))}")
        except Exception as e:  # the map is a convenience; never block the report
            via = f"\n    fade via: (instrument map error: {type(e).__name__})"
    return (f"• {r['symbol']} {r['name']}: {g} index {r['primary_index']:.0f} "
            f"(z {r.get(r['primary'] + '_z', np.nan):+.1f}{wow_s}{spec_s}){px}{via}")


def fund_config() -> dict:
    """config/fund.json: swing_book_usd (null until the book-map decision) and
    risk_pct per trade."""
    import json
    p = ROOT / "config" / "fund.json"
    return json.loads(p.read_text()) if p.exists() else {}


def message(tab: pd.DataFrame) -> str:
    rd = tab["report_date"].max()
    rel = pd.to_datetime(tab["available_at"].max(), utc=True).tz_convert(NY)
    lines = [f"\U0001F4CA COT crowding — positions as of {rd} (public {rel:%a %b %-d})", ""]
    longs = tab[tab["crowded"] == "long"]
    shorts = tab[tab["crowded"] == "short"]
    near = tab[(tab["crowded"] == "") & (tab["extremity"] >= 30)]
    lines.append(f"Crowded LONG (index ≥ {CROWDED_HI}):" if len(longs) else "Crowded LONG: none")
    lines += [fmt_row(r) for _, r in longs.iterrows()]
    lines.append("")
    lines.append(f"Crowded SHORT (index ≤ {CROWDED_LO}):" if len(shorts) else "Crowded SHORT: none")
    lines += [fmt_row(r) for _, r in shorts.iterrows()]
    if len(near):
        lines += ["", "Getting there (index ≥ 80 or ≤ 20): " +
                  ", ".join(f"{r['symbol']} {r['primary_index']:.0f}" for _, r in near.iterrows())]
    lines += ["", "Context, not a signal: the trigger is price failing against the crowd "
              "(news-failure setup)."]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--force-send", action="store_true")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)

    hist = build_history()
    newest = hist["report_date"].max()
    age = (now.date() - datetime.fromisoformat(newest).date()).days
    if age > 12:
        print(f"FAIL: newest COT report is {newest} ({age} days old); the feeds job may be broken")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    hist.to_csv(OUT / "history.csv", index=False, float_format="%.6g")
    tab = latest_table(hist, now)
    tab.to_csv(OUT / "latest.csv", index=False, float_format="%.6g")
    try:
        import instruments
        instruments.table(fund_config().get("risk_pct", 0.5), OUT / "latest.csv").to_csv(
            instruments.OUT, index=False)
    except Exception as e:
        print(f"instrument map not refreshed: {type(e).__name__}: {e}")
    text = message(tab)
    print(text)

    sent_file = OUT / "last_sent.txt"
    rd = tab["report_date"].max()
    already = sent_file.exists() and sent_file.read_text().strip() == rd
    if args.no_send or (already and not args.force_send):
        print(f"\n(not sending: {'--no-send' if args.no_send else 'report ' + rd + ' already sent'})")
        return 0
    sys.path.insert(0, str(ROOT))
    import notify
    if notify.send(text):
        sent_file.write_text(rd + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
