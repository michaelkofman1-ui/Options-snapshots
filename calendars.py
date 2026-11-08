"""
Market-specific catalyst calendars for the news-failure v2 map (approved by
Michael, Nov 8, 2026). Each source is read from the publisher's own page or
file by plain parsing; nothing is typed in by hand.

  eia      Weekly Petroleum Status Report (EIA archive from Aug 2011; the
           standard schedule before that, marked basis="standard schedule")
           Weekly Natural Gas Storage Report (standard schedule, Thursday
           10:30 ET with the federal-holiday shifts)
  usda     WASDE, Grain Stocks, Prospective Plantings, Acreage, Cattle on
           Feed, Hogs and Pigs (USDA ESMIS release lists at Cornell)
  cb       ECB, BoJ, BoE, RBA, BoC, SNB, Banxico policy decisions
  fomc     FOMC meetings 2000-2020 from the Fed's historical pages (fomc.csv
           already covers 2021 on)

OPEC is not here: opec.org refuses automated requests (403). The v2 map
lists it for CL/RB/HO, so its absence is reported, not filled in.

Output: data/external/events/{eia,usda,central_banks,fomc_history}.csv with
columns date, time_et, event, detail, basis.
"""
from __future__ import annotations

import io
import re
import time
from datetime import date, timedelta

import pandas as pd

START = "2006-01-01"
MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


LOG: list[str] = []


def get(url: str) -> bytes:
    from feeds import http_get
    t0 = time.monotonic()
    try:
        body = http_get(url, tries=2, timeout=30)
        LOG.append(f"{time.monotonic() - t0:6.1f}s {len(body):>8} {url}")
        return body
    except Exception as e:
        LOG.append(f"{time.monotonic() - t0:6.1f}s FAIL {type(e).__name__} {url}")
        raise


def _text(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))


def frame(dates, event, time_et, detail="", basis="publisher") -> pd.DataFrame:
    d = sorted({x for x in dates if x >= START})
    return pd.DataFrame({"date": d, "time_et": time_et, "event": event, "detail": detail, "basis": basis})


# ── US federal holidays (EIA schedule shifts) ──────────────────────────────
def federal_holidays(y0=2000, y1=2030) -> set[str]:
    from pandas.tseries.holiday import USFederalHolidayCalendar
    h = USFederalHolidayCalendar().holidays(f"{y0}-01-01", f"{y1}-12-31")
    return set(h.strftime("%Y-%m-%d"))


def wpsr_rule(d0: str, d1: str, hol: set[str]) -> list[str]:
    """Standard schedule: Wednesday; Thursday when a federal holiday falls on
    the Monday-Wednesday of that week."""
    out = []
    for wed in pd.date_range(d0, d1, freq="W-WED"):
        week = [(wed - timedelta(days=k)).strftime("%Y-%m-%d") for k in (2, 1, 0)]
        out.append((wed + timedelta(days=1) if any(x in hol for x in week) else wed).strftime("%Y-%m-%d"))
    return out


def wngsr_rule(d0: str, d1: str, hol: set[str]) -> list[str]:
    """Standard schedule: Thursday 10:30; the Wednesday before when the
    Thursday is a federal holiday (Thanksgiving, July 4, Christmas, New
    Year's)."""
    out = []
    for thu in pd.date_range(d0, d1, freq="W-THU"):
        s = thu.strftime("%Y-%m-%d")
        out.append((thu - timedelta(days=1)).strftime("%Y-%m-%d") if s in hol else s)
    return out


def parse_wpsr_archive(html: str) -> list[str]:
    ds = re.findall(r"/petroleum/supply/weekly/archive/\d{4}/(\d{4})_(\d{2})_(\d{2})/", html)
    return sorted({f"{y}-{m}-{d}" for y, m, d in ds})


def fetch_eia(today: str, full: bool = True) -> pd.DataFrame:
    hol = federal_holidays()
    arch = parse_wpsr_archive(get("https://www.eia.gov/petroleum/supply/weekly/archive/").decode("utf-8", "replace"))
    if len(arch) < 500:
        raise ValueError(f"EIA petroleum archive parse found only {len(arch)} reports")
    first = arch[0]
    early = [d for d in wpsr_rule(START, first, hol) if d < first]
    horizon = (pd.Timestamp(today) + pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    late = [d for d in wpsr_rule(today, horizon, hol) if d > arch[-1]]
    pet = pd.concat([frame(early, "EIA petroleum", "10:30", "Weekly Petroleum Status Report", "standard schedule"),
                     frame(arch, "EIA petroleum", "10:30", "Weekly Petroleum Status Report", "EIA archive"),
                     frame(late, "EIA petroleum", "10:30", "Weekly Petroleum Status Report", "standard schedule")])
    gas = frame(wngsr_rule(START, horizon, hol), "EIA gas storage", "10:30", "Weekly Natural Gas Storage Report",
                "standard schedule")
    return pd.concat([pet, gas], ignore_index=True)


# ── USDA (ESMIS at Cornell) ────────────────────────────────────────────────
USDA = {
    "WASDE": ("3t945q76s", "12:00", "World Agricultural Supply and Demand Estimates"),
    "Grain Stocks": ("xg94hp534", "12:00", "Grain Stocks"),
    "Prospective Plantings": ("x633f100h", "12:00", "Prospective Plantings"),
    "Acreage": ("j098zb09z", "12:00", "Acreage"),
    "Cattle on Feed": ("m326m174z", "15:00", "Cattle on Feed"),
    "Hogs and Pigs": ("rj430453j", "15:00", "Quarterly Hogs and Pigs"),
}


def parse_esmis(html: str) -> tuple[str, list[str]]:
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    rows = re.findall(r'views-field-release-date"><time datetime="(\d{4}-\d{2}-\d{2})', html)
    return (title.group(1).strip() if title else ""), rows


def resolve(url: str) -> str:
    """Final URL after redirects. The Cornell ESMIS links now redirect to
    esmis.nal.usda.gov and drop query strings, so paging needs the new URL."""
    import urllib.request
    from feeds import UA
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.geturl().split("?")[0]


def fetch_usda(today: str, full: bool = True) -> pd.DataFrame:
    parts = []
    for event, (pid, t, must) in USDA.items():
        base = resolve(f"https://usda.library.cornell.edu/concern/publications/{pid}")
        dates, page, first_seen = [], 0, set()
        while page < 60:
            html = get(f"{base}?page={page}").decode("utf-8", "replace")
            title, rows = parse_esmis(html)
            if must.lower() not in title.lower():
                raise ValueError(f"ESMIS {pid} is '{title[:60]}', expected {must}")
            if not rows:
                break
            if rows[0] in first_seen:               # the pager was ignored: stop, don't loop
                raise ValueError(f"ESMIS {event}: page {page} repeats an earlier page")
            first_seen.add(rows[0])
            dates += rows
            if min(rows) < START or not full:
                break
            page += 1
            time.sleep(0.5)
        if full and len([d for d in dates if d >= START]) < 15:
            raise ValueError(f"ESMIS {event}: only {len(dates)} releases")
        parts.append(frame(dates, event, t, must))
    return pd.concat(parts, ignore_index=True)


# ── central banks ──────────────────────────────────────────────────────────
def parse_ecb(html: str) -> list[str]:
    return re.findall(r'<dt isoDate="(\d{4}-\d{2}-\d{2})"', html)


def parse_boj(html: str) -> list[str]:
    return [f"20{y}-{m}-{d}" for y, m, d in re.findall(r"/mpmdeci/[a-z_0-9]+/k(\d{2})(\d{2})(\d{2})[a-z]?\.pdf", html)]


def parse_boe(xlsx: bytes) -> list[str]:
    v = pd.read_excel(io.BytesIO(xlsx), sheet_name="Bank Rate Decisions", header=None)
    out = []
    for c in range(min(4, v.shape[1])):
        for x in v.iloc[:, c]:
            if isinstance(x, (pd.Timestamp,)) or hasattr(x, "year") and hasattr(x, "month") and not isinstance(x, (int, float)):
                out.append(pd.Timestamp(x).strftime("%Y-%m-%d"))
    return out


def parse_rba(html: str) -> list[str]:
    new = re.findall(r"/rba-board-minutes/\d{4}/(\d{4}-\d{2}-\d{2})\.html", html)
    old = [f"{y}-{m}-{d}" for d, m, y in re.findall(r"/rba-board-minutes/\d{4}/(\d{2})(\d{2})(\d{4})\.html", html)]
    return new + old


def parse_boc(html: str) -> list[str]:
    return re.findall(r'data-date="(\d{4}-\d{2}-\d{2})"', html)


def parse_snb(html: str) -> list[str]:
    out = []
    for d, mon, y in re.findall(r"Monetary policy assessment of (\d{1,2}) ([A-Z][a-z]+) (\d{4})", _text(html)):
        out.append(f"{y}-{MON[mon[:3].lower()]:02d}-{int(d):02d}")
    return out


def parse_banxico(html: str) -> list[str]:
    return [f"20{y}-{m}-{d}" for d, m, y in
            re.findall(r'class=bmdateview aria-label="[^"]*"><SPAN aria-hidden=true>(\d{2})/(\d{2})/(\d{2})', html)]


def fetch_central_banks(today: str, full: bool = True) -> pd.DataFrame:
    yr = int(today[:4])
    y0 = int(START[:4]) if full else yr - 1
    parts = []
    ecb = []
    for y in range(y0, yr + 1):
        ecb += parse_ecb(get(f"https://www.ecb.europa.eu/press/govcdec/mopo/{y}/html/index_include.en.html").decode("utf-8", "replace"))
        time.sleep(0.3)
    parts.append(frame(ecb, "ECB", "08:15", "ECB monetary policy decision"))
    boj = []
    for y in range(y0, yr + 1):
        boj += parse_boj(get(f"https://www.boj.or.jp/en/mopo/mpmdeci/state_{y}/index.htm").decode("utf-8", "replace"))
        time.sleep(0.3)
    parts.append(frame(boj, "BoJ", "overnight", "BoJ monetary policy statement (Tokyo midday, the US night before)"))
    parts.append(frame(parse_boe(get("https://www.bankofengland.co.uk/-/media/boe/files/monetary-policy-summary-and-minutes/mpcvoting.xlsx")),
                       "BoE", "07:00", "BoE Bank Rate decision"))
    rba = []
    for y in range(max(2007, y0), yr + 1):
        rba += parse_rba(get(f"https://www.rba.gov.au/monetary-policy/rba-board-minutes/{y}/").decode("utf-8", "replace"))
        time.sleep(0.3)
    # Same-day decision statements began with the December 2007 meeting.
    parts.append(frame([d for d in rba if d >= "2007-12-01"], "RBA", "overnight",
                       "RBA board decision (Sydney afternoon, the US night before)"))
    parts.append(frame(parse_boc(get("https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/history-key-interest-rate/").decode("utf-8", "replace")),
                       "BoC", "10:00", "BoC interest rate announcement"))
    parts.append(frame(parse_snb(get("https://www.snb.ch/en/the-snb/mandates-goals/monetary-policy/decisions").decode("utf-8", "replace")),
                       "SNB", "03:30", "SNB monetary policy assessment"))
    parts.append(frame(parse_banxico(get("https://www.banxico.org.mx/publicaciones-y-prensa/anuncios-de-las-decisiones-de-politica-monetaria/anuncios-politica-monetaria-t.html").decode("latin-1")),
                       "Banxico", "14:00", "Banxico monetary policy announcement"))
    df = pd.concat(parts, ignore_index=True)
    counts = df[df["date"] < f"{yr}-01-01"].groupby("event").size()
    need = 40 if full else 1
    short = [e for e in ["ECB", "BoJ", "BoE", "RBA", "BoC", "SNB", "Banxico"] if counts.get(e, 0) < need]
    if short:
        raise ValueError("central bank parse too short: " + ", ".join(f"{e} {counts.get(e, 0)}" for e in short))
    return df


# ── FOMC history ───────────────────────────────────────────────────────────
def parse_fomc_history(html: str, year: int) -> list[str]:
    out = []
    for h in re.findall(r"<h[3-5][^>]*>([^<]*Meeting[^<]*)</h", html):
        m = re.match(r"\s*([A-Z][a-z]+)(?:/([A-Z][a-z]+))?\s+(\d{1,2})(?:-(\d{1,2}))?\s+(?:\(unscheduled\)\s+)?Meeting", h)
        if not m:
            continue
        m1, m2, d1, d2 = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        mon = MON[(m2 or m1)[:3].lower()]
        day = int(d2) if d2 else d1
        out.append(date(year, mon, day).isoformat())
    return out


def fetch_fomc_history(today: str, full: bool = True) -> pd.DataFrame:
    ds = []
    for y in range(2000, 2021):
        ds += parse_fomc_history(get(f"https://www.federalreserve.gov/monetarypolicy/fomchistorical{y}.htm").decode("utf-8", "replace"), y)
        time.sleep(0.3)
    if len(ds) < 150:
        raise ValueError(f"FOMC history parse found only {len(ds)} meetings")
    return frame(ds, "FOMC", "14:00", "FOMC decision (Fed historical materials)")


SOURCES = {
    "fomc_history": fetch_fomc_history,
    "central_banks": fetch_central_banks,
    "eia": fetch_eia,
    "usda": fetch_usda,
}


if __name__ == "__main__":
    import sys
    from pathlib import Path
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "probe")
    out.mkdir(exist_ok=True, parents=True)
    today = date.today().isoformat()
    for name, fn in SOURCES.items():
        try:
            df = fn(today, True)
            df.to_csv(out / f"{name}.csv", index=False)
            LOG.append(f"== {name} ok {len(df)} {df.groupby('event').size().to_dict()}")
        except Exception as e:
            LOG.append(f"== {name} FAIL {type(e).__name__} {str(e)[:200]}")
        (out / "log.txt").write_text("\n".join(LOG) + "\n")
