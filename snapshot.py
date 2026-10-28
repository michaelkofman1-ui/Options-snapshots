#!/usr/bin/env python3
"""
Daily options-chain snapshot for the fund's data layer.

Source: the free 15-minute-delayed chains Cboe publishes for each symbol in
symbols.txt. No LLM, no API keys. Meant to run from GitHub Actions a few
times each evening. Writes:

  data/chains/YYYY/YYYY-MM-DD/<SYMBOL>.parquet   one file per underlying per day
  data/summary.csv                                one row per underlying per day

Each run only replaces a day's file with a better snapshot (a post-close one
beats an intraday one), so reruns are safe and a missed run heals itself on
the next one. A replacement that would shrink a file by more than half is
refused.

Usage:
  python snapshot.py                    # all symbols in symbols.txt
  python snapshot.py --symbols SPY QQQ  # a subset
  python snapshot.py --require-close    # also fail if today's close is missing
  python snapshot.py --from-file SPY=path/to/SPY.json   # offline test
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CHAINS = DATA / "chains"
SUMMARY = DATA / "summary.csv"

FEED_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
# The feed's top-level "timestamp" is UTC: on 2026-10-28 it read 16:10 while
# New York was 13:31. Per-contract last_trade_time is New York time.
FEED_TZ = timezone.utc
NY = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1

DTE_MAX = 200
ABS_DELTA_MIN = 0.01
ABS_DELTA_MAX = 0.65
MIN_ROWS = 20          # a kept chain smaller than this is treated as broken
MIN_QUOTED_SHARE = 0.3  # at least this share of kept rows must have a live ask
SHRINK_LIMIT = 0.5      # refuse to replace a file with one under half its rows

SESSION_RANK = {"intraday": 1, "close": 2}

OCC = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
NUM_COLS = [
    "bid", "ask", "bid_size", "ask_size", "last_trade_price", "iv", "delta",
    "gamma", "vega", "theta", "rho", "theo", "open_interest", "volume",
    "prev_day_close",
]
OUT_COLS = [
    "option", "root", "expiration", "dte", "cp", "strike", "bid", "ask", "mid",
    "bid_size", "ask_size", "last_trade_price", "last_trade_time", "iv", "delta",
    "gamma", "vega", "theta", "rho", "theo", "open_interest", "volume",
    "prev_day_close",
]


# ── fetch ──────────────────────────────────────────────────────────────────
def fetch(sym: str, tries: int = 4) -> dict:
    url = FEED_URL.format(sym=sym)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (personal research; daily options snapshot)",
        "Accept": "application/json",
    })
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:  # network, HTTP, or JSON error
            last = e
            if i < tries - 1:
                time.sleep(5 * 2 ** i)
    raise RuntimeError(f"fetch failed after {tries} tries: {last}")


# ── timing ─────────────────────────────────────────────────────────────────
def feed_time(payload: dict) -> datetime:
    raw = payload.get("timestamp")
    if not raw:
        raise ValueError("feed has no timestamp")
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=FEED_TZ)


def classify(ts_utc: datetime):
    """Return (session, ny_time). session is None when the stamp can't be
    tied to a regular session: weekends, overnight, or Cboe global trading
    hours (from 20:15 New York)."""
    ny = ts_utc.astimezone(NY)
    t = ny.time()
    if ny.weekday() >= 5 or t < dtime(9, 30) or t >= dtime(20, 15):
        return None, ny
    return ("close" if t >= dtime(16, 0) else "intraday"), ny


# ── parsing ────────────────────────────────────────────────────────────────
def parse_chain(payload: dict, trade_date) -> pd.DataFrame:
    opts = (payload.get("data") or {}).get("options") or []
    df = pd.DataFrame(opts)
    if df.empty or "option" not in df:
        raise ValueError("feed returned no options")
    m = df["option"].astype(str).str.extract(OCC)
    ok = m["root"].notna()
    df, m = df.loc[ok].copy(), m.loc[ok]
    exp = pd.to_datetime(m["exp"], format="%y%m%d")
    df["root"] = m["root"]
    df["expiration"] = exp.dt.strftime("%Y-%m-%d")
    df["cp"] = m["cp"]
    df["strike"] = m["strike"].astype("int64") / 1000.0
    df["dte"] = (exp - pd.Timestamp(trade_date)).dt.days.astype("int16")
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df else np.nan
    if "last_trade_time" not in df:
        df["last_trade_time"] = None
    quoted = (df["ask"] > 0) & (df["bid"] >= 0)
    df["mid"] = np.where(quoted, (df["bid"] + df["ask"]) / 2, np.nan)
    return df


def keep_rows(df: pd.DataFrame) -> pd.DataFrame:
    keep = (
        df["dte"].between(0, DTE_MAX)
        & df["delta"].abs().between(ABS_DELTA_MIN, ABS_DELTA_MAX)
    )
    out = df.loc[keep, OUT_COLS].copy()
    for c in out.columns:
        if out[c].dtype == "float64" and c != "strike":
            out[c] = out[c].astype("float32")
    return out.sort_values(["expiration", "cp", "strike"]).reset_index(drop=True)


def feed_scalars(payload: dict) -> dict:
    """Underlying-level fields Cboe sends alongside the chain (price, iv30...)."""
    data = payload.get("data") or {}
    return {f"feed_{k}": v for k, v in data.items()
            if k != "options" and (v is None or isinstance(v, (int, float, str, bool)))}


# ── summary metrics ────────────────────────────────────────────────────────
def spot_from_parity(df: pd.DataFrame) -> float:
    """Spot ≈ K + C − P at the strike where C and P are closest, nearest
    expiry first. Ignores carry, which is negligible a few days out."""
    q = df[df["mid"] > 0]
    for _, g in sorted(q.groupby("expiration"), key=lambda kv: kv[0]):
        piv = g.pivot_table(index="strike", columns="cp", values="mid", aggfunc="first")
        if not {"C", "P"} <= set(piv.columns):
            continue
        piv = piv.dropna()
        if len(piv) < 3:
            continue
        k = (piv["C"] - piv["P"]).abs().idxmin()
        return float(k + piv.loc[k, "C"] - piv.loc[k, "P"])
    return float("nan")


def _valid_iv(g: pd.DataFrame) -> pd.DataFrame:
    return g[(g["iv"] > 0) & (g["iv"] < 5) & g["delta"].abs().between(0.02, 0.98)]


def iv_at_delta(g: pd.DataFrame, cp: str, target: float) -> float:
    """IV at |delta| == target for one expiry, by linear interpolation."""
    s = _valid_iv(g[g["cp"] == cp])
    if len(s) < 2:
        return float("nan")
    x = s["delta"].abs().to_numpy()
    y = s["iv"].to_numpy()
    order = np.argsort(x)
    x, y = x[order], y[order]
    if not (x[0] <= target <= x[-1]):
        return float("nan")
    return float(np.interp(target, x, y))


def term_interp(points, T: float) -> float:
    """Interpolate IV to T days in total variance (iv² · t). No extrapolation."""
    pts = sorted((d, v) for d, v in points if d >= 2 and np.isfinite(v) and v > 0)
    for d, v in pts:
        if d == T:
            return v
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 < T < d1:
            w0, w1 = v0 * v0 * d0, v1 * v1 * d1
            w = w0 + (w1 - w0) * (T - d0) / (d1 - d0)
            return math.sqrt(w / T) if w > 0 else float("nan")
    return float("nan")


def summarize(df: pd.DataFrame) -> dict:
    by_exp = {d: g for d, g in df.groupby("dte")}
    atm, p25, c25, p16, c16 = [], [], [], [], []
    for d, g in by_exp.items():
        a = [iv_at_delta(g, "C", 0.5), iv_at_delta(g, "P", 0.5)]
        a = [v for v in a if np.isfinite(v)]
        atm.append((d, float(np.mean(a)) if a else float("nan")))
        p25.append((d, iv_at_delta(g, "P", 0.25)))
        c25.append((d, iv_at_delta(g, "C", 0.25)))
        p16.append((d, iv_at_delta(g, "P", 0.16)))
        c16.append((d, iv_at_delta(g, "C", 0.16)))
    s = {
        "atm_iv_30": term_interp(atm, 30),
        "atm_iv_60": term_interp(atm, 60),
        "atm_iv_90": term_interp(atm, 90),
        "put25_iv_30": term_interp(p25, 30),
        "call25_iv_30": term_interp(c25, 30),
        "put16_iv_45": term_interp(p16, 45),
        "call16_iv_45": term_interp(c16, 45),
    }
    s["skew25_30"] = s["put25_iv_30"] - s["call25_iv_30"]
    s["term_90_30"] = s["atm_iv_90"] - s["atm_iv_30"]

    vol = df["volume"].fillna(0)
    oi = df["open_interest"].fillna(0)
    is_c, is_p = df["cp"] == "C", df["cp"] == "P"
    s["call_volume"] = float(vol[is_c].sum())
    s["put_volume"] = float(vol[is_p].sum())
    s["call_oi"] = float(oi[is_c].sum())
    s["put_oi"] = float(oi[is_p].sum())
    tot = s["call_volume"] + s["put_volume"]
    s["pc_volume_ratio"] = s["put_volume"] / s["call_volume"] if s["call_volume"] else float("nan")
    s["zero_dte_volume_share"] = float(vol[df["dte"] == 0].sum()) / tot if tot else float("nan")
    return s


# ── storage ────────────────────────────────────────────────────────────────
def chain_path(sym: str, trade_date: str) -> Path:
    return CHAINS / trade_date[:4] / trade_date / f"{sym.lstrip('_')}.parquet"


def load_summary() -> pd.DataFrame:
    if SUMMARY.exists():
        return pd.read_csv(SUMMARY, dtype={"trade_date": str, "symbol": str})
    return pd.DataFrame(columns=["trade_date", "symbol"])


def save_summary(summ: pd.DataFrame) -> None:
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    summ = summ.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    tmp = SUMMARY.with_suffix(".csv.tmp")
    summ.to_csv(tmp, index=False, float_format="%.6g")
    tmp.replace(SUMMARY)


def is_better(existing: pd.Series | None, session: str, ts_iso: str) -> bool:
    if existing is None:
        return True
    old = (SESSION_RANK.get(existing.get("session"), 0), str(existing.get("feed_ts_utc")))
    return (SESSION_RANK[session], ts_iso) > old


# ── one symbol ─────────────────────────────────────────────────────────────
def process(sym: str, payload: dict, summ: pd.DataFrame, fetched_at: datetime):
    """Returns (status, message, updated summary)."""
    ts = feed_time(payload)
    session, ny = classify(ts)
    if session is None:
        return "skip", f"feed stamped {ny:%a %Y-%m-%d %H:%M} NY, outside a regular session", summ
    trade_date = ny.date().isoformat()
    ts_iso = ts.isoformat()

    mask = (summ["trade_date"] == trade_date) & (summ["symbol"] == sym)
    existing = summ.loc[mask].iloc[0] if mask.any() else None
    if not is_better(existing, session, ts_iso):
        return "same", f"{trade_date} {existing['session']} already saved", summ

    full = parse_chain(payload, trade_date)
    kept = keep_rows(full)
    if len(kept) < MIN_ROWS:
        raise ValueError(f"only {len(kept)} contracts after filtering")
    quoted_share = float((kept["ask"] > 0).mean())
    if quoted_share < MIN_QUOTED_SHARE:
        raise ValueError(f"only {quoted_share:.0%} of contracts have a live ask")

    path = chain_path(sym, trade_date)
    if existing is not None and path.exists():
        old_rows = int(existing.get("n_kept") or 0)
        if old_rows and len(kept) < SHRINK_LIMIT * old_rows:
            raise ValueError(f"refusing to shrink {path.name} from {old_rows} to {len(kept)} rows")

    row = {
        "trade_date": trade_date,
        "symbol": sym,
        "session": session,
        "feed_ts_utc": ts_iso,
        "feed_ts_ny": ny.strftime("%Y-%m-%d %H:%M:%S"),
        "fetched_at_utc": fetched_at.isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "n_raw": int(len(full)),
        "n_kept": int(len(kept)),
        "n_expirations": int(kept["expiration"].nunique()),
        "quoted_share": round(quoted_share, 4),
    }
    scal = feed_scalars(payload)
    try:
        feed_spot = float(scal.get("feed_current_price"))
    except (TypeError, ValueError):
        feed_spot = float("nan")
    row["spot_parity"] = spot_from_parity(full)
    row["spot"] = feed_spot if feed_spot > 0 else row["spot_parity"]
    try:
        row.update(summarize(full))
    except Exception as e:  # metrics are a convenience; never lose the chain over them
        row["summary_error"] = str(e)[:200]
    row.update(scal)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    kept.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)

    summ = pd.concat([summ.loc[~mask], pd.DataFrame([row])], ignore_index=True)
    return "saved", f"{trade_date} {session} @ {ny:%H:%M} NY, {len(kept)} contracts", summ


# ── main ───────────────────────────────────────────────────────────────────
def read_symbols() -> list[str]:
    syms = []
    for line in (ROOT / "symbols.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            syms.append(line.upper())
    return syms


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--require-close", action="store_true",
                    help="fail if today's post-close snapshot is missing for any symbol")
    ap.add_argument("--from-file", nargs="*", default=[],
                    help="SYMBOL=path.json pairs to parse instead of fetching (testing)")
    args = ap.parse_args(argv)

    files = dict(kv.split("=", 1) for kv in args.from_file)
    syms = [s.upper() for s in (args.symbols or list(files) or read_symbols())]
    summ = load_summary()
    results = []
    for sym in syms:
        fetched_at = datetime.now(timezone.utc)
        try:
            payload = (json.loads(Path(files[sym]).read_text()) if sym in files
                       else fetch(sym))
            status, msg, summ = process(sym, payload, summ, fetched_at)
        except Exception as e:
            status, msg = "FAIL", str(e)[:300]
        results.append((sym, status, msg))
        print(f"{sym:6s} {status:5s} {msg}", flush=True)
    save_summary(summ)

    failed = [r for r in results if r[1] == "FAIL"]

    if args.require_close:
        today = datetime.now(NY).date()
        if today.weekday() < 5:
            d = today.isoformat()
            have = (set(summ.loc[(summ["trade_date"] == d) & (summ["session"] == "close"), "symbol"])
                    if "session" in summ else set())
            if have:  # nothing at all means the market was closed today
                for sym in syms:
                    if sym not in have:
                        failed.append((sym, "FAIL", f"no post-close snapshot for {d}"))
                        print(f"{sym:6s} FAIL  no post-close snapshot for {d}")
            else:
                print(f"No post-close snapshots for {d}; treating it as a market holiday.")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write("| Symbol | Status | Detail |\n|---|---|---|\n")
            for sym, status, msg in results + [r for r in failed if r not in results]:
                f.write(f"| {sym} | {status} | {msg} |\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
