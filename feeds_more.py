"""
More external feeds, called from feeds.py:

  bars    daily OHLCV for ETFs, indexes, AI names and crypto (Yahoo via yfinance)
          -> data/external/bars/<TICKER>.csv
  events  one calendar of catalysts -> data/external/events/
            macro.csv     CPI, jobs, PPI, retail sales, GDP, PCE, JOLTS, claims
                          release dates from FRED (history and scheduled)
            fomc.csv      FOMC decision days from federalreserve.gov (2021 on)
            earnings.csv  earnings dates, EPS estimate/actual, surprise % for
                          the AI and mega-cap names (Yahoo)
            events.csv    all of the above in one table

Bars store Yahoo's split-adjusted Close plus dividends and splits, not the
dividend-adjusted close, which Yahoo rewrites on every dividend. When a split
shows up, that ticker's whole history is reloaded and allowed to change.
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

BARS = [
    # broad market and factors
    "QQQ", "SPY", "IWM", "DIA", "^NDX", "^GSPC", "^RUT",
    # AI / semis
    "SMH", "SOXX", "XLK", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "AAPL", "AVGO",
    "TSLA", "AMD", "TSM", "ORCL", "PLTR",
    # rates, credit, dollar
    "TLT", "IEF", "SHY", "HYG", "LQD", "UUP",
    # commodities and other proxies
    "GLD", "SLV", "USO", "UNG", "DBA", "XLE", "XLF", "EEM", "FXI",
    # crypto
    "BTC-USD", "ETH-USD", "SOL-USD",
    # overseas legs of the AI trade, for cross-market news failures
    # (Oct 28, 2026: KOSPI +6.6% on SK hynix results, Nasdaq-100 +0.55%)
    "^KS11", "000660.KS", "005930.KS", "^TWII", "2330.TW", "EWY", "EWT", "ASML",
]
EARNINGS = ["NVDA", "MSFT", "GOOGL", "AMZN", "META", "AAPL", "AVGO", "TSLA", "AMD",
            "TSM", "ORCL", "PLTR"]

# FRED release id -> (our label, a word its official name must contain, NY release time)
MACRO_RELEASES = {
    10: ("CPI", "Consumer Price", "08:30"),
    50: ("Jobs report", "Employment Situation", "08:30"),
    46: ("PPI", "Producer Price", "08:30"),
    9: ("Retail sales", "Retail", "08:30"),
    53: ("GDP", "Gross Domestic Product", "08:30"),
    54: ("PCE / personal income", "Personal Income", "08:30"),
    192: ("JOLTS", "Job Openings", "10:00"),
    180: ("Jobless claims", "Unemployment Insurance", "08:30"),
}
FRED_API = "https://api.stlouisfed.org/fred"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
ABBR = {m[:3]: i for m, i in MONTHS.items()}


def bar_file(ticker: str) -> str:
    return ticker.replace("^", "IDX_") + ".csv"


# ── bars ───────────────────────────────────────────────────────────────────
def _frame(raw: pd.DataFrame, t: str, multi: bool) -> pd.DataFrame:
    d = (raw[t] if multi else raw).copy()
    d.columns = [str(c).lower() for c in d.columns]
    d = d.rename(columns={"stock splits": "splits"})
    keep = [c for c in ["open", "high", "low", "close", "volume", "dividends", "splits"] if c in d]
    d = d[keep].dropna(subset=["close"])
    idx = pd.to_datetime(d.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    d.index = idx.strftime("%Y-%m-%d")
    d.index.name = "date"
    return d.reset_index()


def fetch_bars(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    raw = yf.download(tickers, period=period, interval="1d", auto_adjust=False,
                      actions=True, group_by="ticker", progress=False, threads=True)
    multi = isinstance(raw.columns, pd.MultiIndex)
    out = {}
    for t in tickers:
        try:
            f = _frame(raw, t, multi)
        except KeyError:
            continue
        if not f.empty:
            out[t] = f
    return out


# ── macro release calendar (FRED) ──────────────────────────────────────────
def _fred(path: str, **params) -> dict:
    from feeds import http_get
    params.update(api_key=os.environ["FRED_API_KEY"].strip(), file_type="json")
    return json.loads(http_get(f"{FRED_API}/{path}?{urllib.parse.urlencode(params)}"))


def fetch_macro() -> pd.DataFrame:
    rows = []
    for rid, (label, must, t) in MACRO_RELEASES.items():
        name = _fred("release", release_id=rid)["releases"][0]["name"]
        if must.lower() not in name.lower():
            raise ValueError(f"FRED release {rid} is '{name}', expected {must}")
        dates = _fred("release/dates", release_id=rid, realtime_start="2000-01-01",
                      realtime_end="9999-12-31", include_release_dates_with_no_data="true",
                      sort_order="asc", limit=10000)["release_dates"]
        rows += [{"date": d["date"], "time_et": t, "event": label, "detail": name} for d in dates]
    df = pd.DataFrame(rows)
    return df[df["date"] >= "2000-01-01"]


# ── FOMC ───────────────────────────────────────────────────────────────────
def parse_fomc(html: str) -> pd.DataFrame:
    """Decision day (last day) of each meeting, from the Fed's calendar page.
    Works on tag-stripped text, so layout changes matter less. Dates followed
    by a year ('Released February 18, 2026') are minutes releases, skipped."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|\s+", " ", text)
    month = (r"(January|February|March|April|May|June|July|August|September|October|"
             r"November|December|[A-Z][a-z]{2}/[A-Z][a-z]{2})")
    pat = re.compile(month + r"\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?(\*?)(?!\s*,\s*\d{4})(?!\d)")
    rows = []
    parts = re.split(r"(\d{4}) FOMC Meetings", text)
    for i in range(1, len(parts) - 1, 2):
        year, body = int(parts[i]), parts[i + 1]
        for m in pat.finditer(body):
            mon, d1, d2, star = m.group(1), int(m.group(2)), m.group(3), m.group(4)
            if "/" in mon:
                m1, m2 = (ABBR[x] for x in mon.split("/"))
            else:
                m1 = m2 = MONTHS[mon]
            if d2 is None:
                day, mo = d1, m1
            else:
                day = int(d2)
                mo = m2 if (m2 != m1 and int(d2) < d1) else m1
            tail = body[m.end():m.end() + 40].lower()
            kind = ("unscheduled" if "unscheduled" in tail else
                    "notation vote" if "notation" in tail else "scheduled")
            try:
                dt = date(year, mo, day)
            except ValueError:
                continue
            rows.append({"date": dt.isoformat(), "time_et": "14:00", "event": "FOMC",
                         "detail": kind + (", with projections (SEP)" if star else "")})
    df = pd.DataFrame(rows).drop_duplicates("date") if rows else pd.DataFrame()
    if df.empty or df["date"].str[:4].nunique() < 3:
        raise ValueError("FOMC page parse found too few meetings; layout may have changed")
    return df


def fetch_fomc() -> pd.DataFrame:
    from feeds import http_get
    return parse_fomc(http_get(FOMC_URL).decode("utf-8", "replace"))


# ── earnings ───────────────────────────────────────────────────────────────
def fetch_earnings(tickers: list[str]) -> pd.DataFrame:
    import yfinance as yf
    rows, errors = [], []
    for t in tickers:
        try:
            e = yf.Ticker(t).get_earnings_dates(limit=60)
        except Exception as ex:
            errors.append(f"{t}: {type(ex).__name__}")
            continue
        if e is None or e.empty:
            errors.append(f"{t}: none")
            continue
        e = e.reset_index()
        ts = pd.to_datetime(e.iloc[:, 0])
        ts = ts.dt.tz_convert("America/New_York") if ts.dt.tz is not None else ts
        for i, r in e.iterrows():
            when = ts.iloc[i]
            hm = when.hour * 60 + when.minute
            # These names report outside market hours; a mid-session time is
            # Yahoo's placeholder for a date whose time isn't confirmed yet.
            session = ("before open" if hm < 9 * 60 + 30 else
                       "after close" if hm >= 16 * 60 else "time not confirmed")
            rows.append({
                "symbol": t, "date": when.strftime("%Y-%m-%d"), "time_et": when.strftime("%H:%M"),
                "session": session,
                "eps_estimate": r.get("EPS Estimate"), "eps_reported": r.get("Reported EPS"),
                "surprise_pct": r.get("Surprise(%)"),
            })
    if not rows:
        raise RuntimeError("no earnings data: " + "; ".join(errors[:5]))
    df = pd.DataFrame(rows)
    df.attrs["errors"] = errors
    return df


def merge_calendar(path: Path, new: pd.DataFrame, keys: list[str], today: str) -> tuple[str, str]:
    """Past rows are history and are never dropped. Future rows mirror the
    source, so a rescheduled release doesn't leave a ghost on the old date."""
    if new.empty:
        raise ValueError("source returned no rows")
    new = new.astype({k: str for k in keys}).drop_duplicates(keys, keep="last")
    old = pd.read_csv(path, dtype={k: str for k in keys}) if path.exists() else new.iloc[:0]
    old_past = old[old["date"] <= today]
    merged = pd.concat([old_past, new], ignore_index=True).drop_duplicates(keys, keep="last")
    merged = merged.sort_values(keys).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_bytes() if path.exists() else b""
    tmp = path.with_suffix(".csv.tmp")
    merged.to_csv(tmp, index=False, float_format="%.6g")
    if tmp.read_bytes() == before:
        tmp.unlink()
        return "same", f"{len(merged)} rows, unchanged"
    tmp.replace(path)
    n_future = int((merged["date"] > today).sum())
    return "saved", f"{len(merged)} rows ({n_future} upcoming)"


def combine_events(ev_dir: Path, today: str) -> pd.DataFrame:
    parts = []
    for name in ["macro", "fomc"]:
        p = ev_dir / f"{name}.csv"
        if p.exists():
            parts.append(pd.read_csv(p, dtype=str).assign(symbol=""))
    p = ev_dir / "earnings.csv"
    if p.exists():
        e = pd.read_csv(p, dtype=str)
        parts.append(pd.DataFrame({
            "date": e["date"], "time_et": e["time_et"], "event": "Earnings",
            "symbol": e["symbol"],
            "detail": e["session"] + e["surprise_pct"].fillna("").map(
                lambda s: f", surprise {float(s):+.1f}%" if s else ""),
        }))
    if not parts:
        return pd.DataFrame()
    ev = pd.concat(parts, ignore_index=True)[["date", "time_et", "event", "symbol", "detail"]]
    ev["scheduled"] = ev["date"] > today
    return ev.sort_values(["date", "time_et", "event", "symbol"]).reset_index(drop=True)
