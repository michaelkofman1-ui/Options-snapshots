#!/usr/bin/env python3
"""
External data feeds for the fund (cloud collector).

Deterministic, idempotent fetchers from primary sources. No LLM, no API keys.
Runs from GitHub Actions and commits CSVs under data/external/:

  vol/<INDEX>.csv            Cboe volatility indices, full daily history
  fred/<SERIES>.csv          FRED series (credit, rates, liquidity, NASDAQ-100)
  cot/<report>/<YEAR>.csv    CFTC Commitments of Traders, futures only, by year
  cot/markets.csv            contract code -> market name and group
  cot/releases.csv           when each report date became public

Rules
- Never drops a row: if a source truncates its history (FRED's ICE spreads did
  in 2023), the rows we already hold are kept and the run reports a warning.
- Refuses an update that changes more than 2% of already-held values, unless the
  series is known to revise its history (NFCI). That catches a source returning
  garbage instead of silently overwriting good data.
- Point-in-time: git history is the vintage record. Any file as of any commit is
  exactly what we knew then, so revised series can be replayed without lookahead.

Usage:
  python feeds.py                 # everything
  python feeds.py --only vol fred # a subset
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
EXT = ROOT / "data" / "external"
NY = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (personal research; daily market data collector)"
REVISION_LIMIT = 0.02

# ── sources ────────────────────────────────────────────────────────────────
CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv"
VOL_INDICES = [
    "VIX", "VIX9D", "VIX3M", "VIX6M", "VVIX", "VXN", "RVX", "SKEW",
    "GVZ", "OVX", "VXEEM",
]

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
# series id -> revises its own history (skip the 2% revision guard)
FRED_SERIES = {
    # credit (Moody's back to the 1980s; ICE OAS only ~3 years since 2023)
    "BAA10Y": False, "AAA10Y": False, "BAMLH0A0HYM2": False, "BAMLC0A0CM": False,
    # equities with long history
    "NASDAQ100": False, "NASDAQCOM": False,
    # rates and inflation expectations
    "DGS3MO": False, "DGS2": False, "DGS10": False, "T10Y2Y": False,
    "T10Y3M": False, "DFII10": False, "T10YIE": False,
    # financial conditions and liquidity
    "NFCI": True, "ANFCI": True, "WALCL": False, "RRPONTSYD": False, "WTREGEN": False,
    # dollar, oil, labor
    "DTWEXBGS": False, "DCOILWTICO": False, "ICSA": True,
}

COT_API = "https://publicreporting.cftc.gov/resource/{ds}.json"
COT_FIRST_YEAR = 2006  # TFF and disaggregated reports start in 2006
COT_META = ["market_and_exchange_names", "commodity_name", "commodity_group_name",
            "commodity_subgroup_name", "contract_units"]
COT_REPORTS = {
    "legacy": ("6dca-aqww", [
        "open_interest_all",
        "noncomm_positions_long_all", "noncomm_positions_short_all", "noncomm_postions_spread_all",
        "comm_positions_long_all", "comm_positions_short_all",
        "nonrept_positions_long_all", "nonrept_positions_short_all",
        "traders_noncomm_long_all", "traders_noncomm_short_all",
    ]),
    "tff": ("gpe5-46if", [
        "open_interest_all",
        "dealer_positions_long_all", "dealer_positions_short_all",
        "asset_mgr_positions_long", "asset_mgr_positions_short",
        "lev_money_positions_long", "lev_money_positions_short", "lev_money_positions_spread",
        "other_rept_positions_long", "other_rept_positions_short",
        "nonrept_positions_long_all", "nonrept_positions_short_all",
    ]),
    "disagg": ("72hh-3qpy", [
        "open_interest_all",
        "prod_merc_positions_long", "prod_merc_positions_short",
        "swap_positions_long_all", "swap__positions_short_all",
        "m_money_positions_long_all", "m_money_positions_short_all", "m_money_positions_spread",
        "other_rept_positions_long", "other_rept_positions_short",
        "nonrept_positions_long_all", "nonrept_positions_short_all",
    ]),
}
COT_REFRESH_DAYS = 63  # re-pull the last 9 weeks every run to catch corrections
# The first backfill (2006 on) is slow through CFTC's API, so each run spends at
# most this long on missing years, newest first, commits, and the next run
# continues. The current year is always refreshed.
BACKFILL_BUDGET_S = 900
RUN_STARTED = time.monotonic()


# ── http ───────────────────────────────────────────────────────────────────
def http_get(url: str, tries: int = 4, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(5 * 2 ** i)
    raise RuntimeError(f"GET failed after {tries} tries: {last}")


# ── safe merge ─────────────────────────────────────────────────────────────
class Refused(Exception):
    pass


def merge_store(path: Path, new: pd.DataFrame, keys: list[str], revises: bool = False,
                partial: bool = False) -> tuple[str, str]:
    """Union new rows into the CSV at path. Returns (status, message).
    status: saved | same | warn (saved, but the source dropped rows we keep).
    partial=True means `new` is only a recent window, so missing older rows
    are expected rather than a sign the source truncated its history."""
    if new.empty:
        raise Refused("source returned no rows")
    new = new.drop_duplicates(keys, keep="last")
    if path.exists():
        old = pd.read_csv(path, dtype={k: str for k in keys})
    else:
        old = pd.DataFrame(columns=new.columns)

    for k in keys:
        new[k] = new[k].astype(str)
    vals = [c for c in new.columns if c not in keys]
    note = ""
    if not old.empty:
        o = old.set_index(keys)
        n = new.set_index(keys)
        common = o.index.intersection(n.index)
        dropped = 0 if partial else len(o.index.difference(n.index))
        shared_cols = [c for c in vals if c in o.columns]
        if len(common) and shared_cols:
            a = o.loc[common, shared_cols].apply(pd.to_numeric, errors="coerce")
            b = n.loc[common, shared_cols].apply(pd.to_numeric, errors="coerce")
            changed = ~np.isclose(a.to_numpy(float), b.to_numpy(float),
                                  rtol=1e-6, atol=1e-9, equal_nan=True)
            share = float(changed.mean())
            if share > REVISION_LIMIT and not revises:
                raise Refused(f"{share:.1%} of held values would change; kept old data")
            if changed.any():
                note += f"{int(changed.sum())} values revised. "
        if dropped:
            note += f"source no longer has {dropped} rows we hold; kept them. "
        merged = pd.concat([o.loc[o.index.difference(n.index)], n]).reset_index()
    else:
        merged = new

    merged = merged.sort_values(keys).reset_index(drop=True)
    cols = keys + [c for c in merged.columns if c not in keys]
    merged = merged[cols]

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    merged.to_csv(tmp, index=False, float_format="%.10g")
    truncated = "no longer has" in note
    before = path.read_bytes() if path.exists() else b""
    if before == tmp.read_bytes():
        tmp.unlink()
        return ("warn" if truncated else "same"), f"{len(merged)} rows, unchanged. {note}".strip()
    tmp.replace(path)
    added = len(merged) - len(old)
    return ("warn" if truncated else "saved"), f"{len(merged)} rows (+{added}). {note}".strip()


# ── Cboe volatility indices ────────────────────────────────────────────────
def fetch_vol(sym: str) -> pd.DataFrame:
    raw = http_get(CBOE_URL.format(sym=sym))
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" not in df.columns:
        raise ValueError(f"unexpected columns {list(df.columns)}")
    df["date"] = pd.to_datetime(df["date"], format="%m/%d/%Y").dt.strftime("%Y-%m-%d")
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if sym.lower() in df.columns and "close" not in df.columns:
        df = df.rename(columns={sym.lower(): "close"})
    return df.dropna(subset=[c for c in df.columns if c != "date"], how="all")


# ── FRED ───────────────────────────────────────────────────────────────────
def fetch_fred(sid: str) -> pd.DataFrame:
    raw = http_get(FRED_URL.format(sid=sid))
    df = pd.read_csv(io.BytesIO(raw))
    if df.shape[1] < 2:
        raise ValueError("unexpected FRED csv")
    df = df.iloc[:, :2]
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df


# ── CFTC Commitments of Traders ────────────────────────────────────────────
def cot_release_nominal(report_date: date) -> datetime:
    """Positions as of Tuesday are published Friday 3:30 PM ET. Federal
    holidays push it later; first_seen_utc in releases.csv records the truth
    for reports collected live."""
    return datetime.combine(report_date + timedelta(days=3), dtime(15, 30), tzinfo=NY)


def fetch_cot(ds: str, cols: list[str], start: date, end: date) -> pd.DataFrame:
    select = ["report_date_as_yyyy_mm_dd", "cftc_contract_market_code"] + COT_META + cols
    rows, off, limit = [], 0, 50000
    while True:
        q = {
            "$select": ",".join(select),
            "$where": (f"report_date_as_yyyy_mm_dd >= '{start.isoformat()}T00:00:00' AND "
                       f"report_date_as_yyyy_mm_dd < '{end.isoformat()}T00:00:00'"),
            "$order": "report_date_as_yyyy_mm_dd,cftc_contract_market_code",
            "$limit": str(limit),
            "$offset": str(off),
        }
        page = json.loads(http_get(COT_API.format(ds=ds) + "?" + urllib.parse.urlencode(q)))
        rows.extend(page)
        if len(page) < limit:
            break
        off += limit
    df = pd.DataFrame(rows, columns=select)
    if df.empty:
        return df
    df = df.rename(columns={"report_date_as_yyyy_mm_dd": "report_date",
                            "cftc_contract_market_code": "code"})
    df["report_date"] = df["report_date"].str[:10]
    df["code"] = df["code"].str.strip()
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def update_cot_markets(meta: pd.DataFrame, report: str) -> None:
    path = EXT / "cot" / "markets.csv"
    m = meta.sort_values("report_date").drop_duplicates("code", keep="last")
    m = m[["code"] + COT_META].assign(report=report, last_seen=m["report_date"])
    old = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame(columns=m.columns)
    out = pd.concat([old, m.astype(str)], ignore_index=True)
    out = (out.sort_values("last_seen")
              .drop_duplicates(["code", "report"], keep="last")  # newest name wins
              .sort_values(["report", "code"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)


def update_cot_releases(report_dates: set[str], now: datetime) -> None:
    path = EXT / "cot" / "releases.csv"
    old = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame(
        columns=["report_date", "release_nominal_et", "first_seen_utc", "backfilled"])
    have = set(old["report_date"])
    add = []
    for d in sorted(report_dates - have):
        nominal = cot_release_nominal(date.fromisoformat(d))
        late = now - nominal.astimezone(timezone.utc) > timedelta(days=4)
        add.append({"report_date": d,
                    "release_nominal_et": nominal.strftime("%Y-%m-%d %H:%M"),
                    "first_seen_utc": now.isoformat(timespec="seconds"),
                    "backfilled": str(bool(late))})
    if add:
        out = pd.concat([old, pd.DataFrame(add)], ignore_index=True).sort_values("report_date")
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(path, index=False)


def run_cot(report: str, now: datetime) -> list[tuple[str, str, str]]:
    ds, cols = COT_REPORTS[report]
    results = []
    today = now.date()
    years = range(today.year, COT_FIRST_YEAR - 1, -1)
    refresh_from = today - timedelta(days=COT_REFRESH_DAYS)
    seen_dates: set[str] = set()
    for y in years:
        path = EXT / "cot" / report / f"{y}.csv"
        if path.exists() and date(y, 12, 31) < refresh_from:
            continue  # closed year already held
        start = date(y, 1, 1) if not path.exists() else max(date(y, 1, 1), refresh_from)
        end = date(y + 1, 1, 1)
        name = f"cot/{report}/{y}"
        if not path.exists() and y != today.year and \
                time.monotonic() - RUN_STARTED > BACKFILL_BUDGET_S:
            results.append((name, "todo", "backfill deferred to the next run"))
            continue
        try:
            df = fetch_cot(ds, cols, start, end)
            if df.empty:
                results.append((name, "same", "no rows in window"))
                continue
            update_cot_markets(df[["report_date", "code"] + COT_META], report)
            seen_dates |= set(df["report_date"])
            status, msg = merge_store(path, df[["report_date", "code"] + cols],
                                      ["report_date", "code"], partial=path.exists())
            results.append((name, status, msg))
        except Exception as e:
            results.append((name, "FAIL", str(e)[:300]))
    if seen_dates:
        update_cot_releases(seen_dates, now)
    return results


# ── main ───────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=["vol", "fred", "cot"])
    args = ap.parse_args(argv)
    which = set(args.only or ["vol", "fred", "cot"])
    now = datetime.now(timezone.utc)
    results: list[tuple[str, str, str]] = []

    def run(name, fn, path, keys, revises=False):
        try:
            status, msg = merge_store(path, fn(), keys, revises)
        except Exception as e:
            status, msg = "FAIL", str(e)[:300]
        results.append((name, status, msg))
        print(f"{name:22s} {status:5s} {msg}", flush=True)

    if "vol" in which:
        for sym in VOL_INDICES:
            run(f"vol/{sym}", lambda s=sym: fetch_vol(s), EXT / "vol" / f"{sym}.csv", ["date"])
    if "fred" in which:
        for sid, revises in FRED_SERIES.items():
            run(f"fred/{sid}", lambda s=sid: fetch_fred(s), EXT / "fred" / f"{sid}.csv",
                ["date"], revises)
            time.sleep(0.5)
    if "cot" in which:
        for report in COT_REPORTS:
            for r in run_cot(report, now):
                results.append(r)
                print(f"{r[0]:22s} {r[1]:5s} {r[2]}", flush=True)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write("| Feed | Status | Detail |\n|---|---|---|\n")
            for name, status, msg in results:
                f.write(f"| {name} | {status} | {msg} |\n")

    return 1 if any(s == "FAIL" for _, s, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
