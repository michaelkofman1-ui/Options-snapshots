#!/usr/bin/env python3
"""
Swing-book instrument map: how a futures signal gets traded at Robinhood.

Signals come from futures (COT + futures price). Each market gets:
  - a Robinhood micro future, if one is listed (rh_status says how sure we are)
  - an ETF proxy and how good its option chain is (weekly / monthly / none)
  - the rule that picks between them (Part 2 of every alert)

Rule (decided Oct 26): use the micro future when it's listed AND one contract's
stop risk fits the per-trade risk budget; otherwise the ETF proxy (shares for
longs; puts or put debit spreads for shorts, since Robinhood short selling
isn't assumed); markets with neither are watch-only. ETF trades go in the
taxable account, not the IRA.

  python instruments.py [--book 25000] [--risk-pct 0.5]
      writes data/derived/instruments.csv with each micro's dollar risk per
      contract and the smallest book that can trade one

The stop is provisional: 2 x daily ATR(14), until the news-failure and
crowding backtests give a median stop per market.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
LATEST = ROOT / "data" / "derived" / "crowding" / "latest.csv"
OUT = ROOT / "data" / "derived" / "instruments.csv"
STOP_ATR = 2.0
DEFAULT_RISK_PCT = 0.5

# rh_status: confirmed = Michael checked it in the Robinhood app (Oct 31, 2026);
# none = no micro at Robinhood.
# etf_chain (checked via the Robinhood connector Oct 30): weekly = weekly
# expirations (liquid); monthly = monthly only (thin: check spreads);
# index = cash-settled index options.
# symbol: (rh_future, rh_status, $ per 1.0 price move per micro, etf, etf_chain, note)
MAP = {
    "ES": ("MES", "confirmed", 5, "SPY", "weekly", ""),
    "NQ": ("MNQ", "confirmed", 2, "QQQ", "weekly", ""),
    "RTY": ("M2K", "confirmed", 5, "IWM", "weekly", ""),
    "YM": ("MYM", "confirmed", 0.5, "DIA", "weekly", ""),
    "VX": ("", "none", None, "VIX", "index", "VIX index options (and VIXW weeklies); no shares"),
    "ZT": ("", "none", None, "SHY", "monthly", ""),
    "ZF": ("", "none", None, "IEI", "monthly", ""),
    "ZN": ("", "none", None, "IEF", "weekly", ""),
    "TN": ("", "none", None, "IEF", "weekly", ""),
    "ZB": ("", "none", None, "TLT", "weekly", ""),
    "UB": ("", "none", None, "TLT", "weekly", ""),
    "6E": ("M6E", "confirmed", 12_500, "FXE", "monthly", ""),
    "6J": ("MJY", "confirmed", 1_250_000, "FXY", "monthly", ""),
    "6B": ("M6B", "confirmed", 6_250, "FXB", "monthly", ""),
    "6A": ("M6A", "confirmed", 10_000, "FXA", "monthly", ""),
    "6C": ("MCD", "confirmed", 10_000, "FXC", "monthly", ""),
    "6S": ("MSF", "confirmed", 12_500, "FXF", "monthly", ""),
    "6M": ("", "none", None, "", "none", "no micro, no ETF: watch-only"),
    "DX": ("", "none", None, "UUP", "monthly", ""),
    "BTC": ("MBT", "confirmed", 0.1, "IBIT", "weekly", ""),
    "ETH": ("MET", "confirmed", 0.1, "ETHA", "weekly", ""),
    "CL": ("MCL", "confirmed", 100, "USO", "weekly", "roll drag on multi-week holds"),
    "NG": ("MNG", "confirmed", 1_000, "UNG", "weekly", "heavy roll drag on multi-week holds"),
    "RB": ("", "none", None, "UGA", "monthly", "roll drag"),
    "HO": ("", "none", None, "", "none", "no ETF: watch-only"),
    "GC": ("MGC", "confirmed", 10, "GLD", "weekly", ""),
    "SI": ("SIL", "confirmed", 1_000, "SLV", "weekly", ""),
    "HG": ("MHG", "confirmed", 2_500, "CPER", "monthly", ""),
    "PL": ("", "none", None, "PPLT", "monthly", ""),
    "ZC": ("", "none", None, "CORN", "monthly", "roll drag"),
    "ZS": ("", "none", None, "SOYB", "monthly", "roll drag"),
    "ZW": ("", "none", None, "WEAT", "monthly", "roll drag"),
    "KE": ("", "none", None, "WEAT", "monthly", "WEAT tracks Chicago wheat, not KC: loose proxy"),
    "ZL": ("", "none", None, "", "none", "no ETF: watch-only"),
    "ZM": ("", "none", None, "", "none", "no ETF: watch-only"),
    "KC": ("", "none", None, "", "none", "no listed coffee ETF: watch-only"),
    "SB": ("", "none", None, "CANE", "monthly", "roll drag"),
    "CC": ("", "none", None, "", "none", "no listed cocoa ETF: watch-only"),
    "CT": ("", "none", None, "", "none", "no listed cotton ETF: watch-only"),
    "LE": ("", "none", None, "", "none", "no ETF"),
    "HE": ("", "none", None, "", "none", "no ETF"),
}


# Index markets also have E-nano contracts at Robinhood (CME, launched Aug
# 2026): 1/10 of a micro, so they fit books 10x smaller. New and likely thinner,
# so check the spread before trading.
NANO = {"ES": ("NES", 0.5), "NQ": ("NNQ", 0.2), "RTY": ("N2K", 0.5), "YM": ("NDOW", 0.05)}


def table(risk_pct: float = DEFAULT_RISK_PCT, latest: Path = LATEST) -> pd.DataFrame:
    px = pd.read_csv(latest) if latest.exists() else pd.DataFrame()
    if not {"symbol", "close", "atr14"} <= set(px.columns):  # no price context this week
        px = pd.DataFrame(columns=["symbol", "close", "atr14"])
    rows = []
    for sym, (fut, status, mult, etf, chain, note) in MAP.items():
        r = px[px["symbol"] == sym]
        atr = float(r["atr14"].iloc[0]) if len(r) and pd.notna(r["atr14"].iloc[0]) else None
        risk = STOP_ATR * atr * mult if (atr and mult) else None
        nano, nmult = NANO.get(sym, ("", None))
        nrisk = STOP_ATR * atr * nmult if (atr and nmult) else None
        rows.append({
            "symbol": sym, "rh_future": fut, "rh_status": status, "micro_multiplier": mult,
            "atr14": atr, "stop_usd_per_micro": round(risk) if risk else None,
            "min_book_usd": round(risk / (risk_pct / 100), -3) if risk else None,
            "nano": nano, "nano_multiplier": nmult,
            "stop_usd_per_nano": round(nrisk) if nrisk else None,
            "min_book_nano_usd": round(nrisk / (risk_pct / 100), -3) if nrisk else None,
            "etf": etf, "etf_chain": chain, "note": note,
        })
    return pd.DataFrame(rows)


def expression(sym: str, direction: str, book_usd: float | None = None,
               risk_pct: float = DEFAULT_RISK_PCT, tab: pd.DataFrame | None = None) -> str:
    """Part 2 of an alert: how to put the trade on at Robinhood."""
    t = (tab if tab is not None else table(risk_pct)).set_index("symbol").loc[sym]
    fut_ok = t["rh_status"] in ("confirmed", "reported") and pd.notna(t["stop_usd_per_micro"])
    if fut_ok and book_usd and book_usd >= t["min_book_usd"]:
        return f"{t['rh_future']} micro future ({direction}), 1 contract risks ~${t['stop_usd_per_micro']:,.0f}"
    nano_ok = isinstance(t.get("nano"), str) and bool(t.get("nano")) and pd.notna(t.get("stop_usd_per_nano"))
    if nano_ok and book_usd and book_usd >= t["min_book_nano_usd"]:
        return (f"{t['nano']} nano future ({direction}), 1 contract risks ~${t['stop_usd_per_nano']:,.0f}; "
                f"new contract, check the spread")
    if t["etf"] and t["etf_chain"] != "none":
        if t["etf_chain"] == "index":
            how = "call spread" if direction == "long" else "put spread"
            return f"{t['etf']} {how}"
        if direction == "long":
            how = "shares"
        elif t["etf_chain"] == "weekly":
            how = "puts or a put debit spread"
        else:
            how = "small put debit spread, limit orders (thin chain)"
        extra = f"; {t['note']}" if t["note"] else ""
        why = ""
        if nano_ok:
            why = f" (one {t['nano']} needs a ~${t['min_book_nano_usd']:,.0f} book)"
        elif fut_ok:
            why = f" (one {t['rh_future']} needs a ~${t['min_book_usd']:,.0f} book)"
        return f"{t['etf']} {how}{why}{extra}"
    return "watch-only" + (f": {t['note']}" if t["note"] else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk-pct", type=float, default=DEFAULT_RISK_PCT)
    args = ap.parse_args(argv)
    tab = table(args.risk_pct)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tab.to_csv(OUT, index=False)
    print(tab.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
