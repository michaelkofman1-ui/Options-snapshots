# Options-snapshots

The fund's cloud data collector: daily option-chain snapshots plus the public
external feeds (vol indices, COT, FRED). Everything here runs in GitHub
Actions, with no laptop and no LLM in the data path. Sierra Chart intraday
data stays on the gaming-laptop collector.

## Option-chain snapshots

Builds the fund's own options history: one snapshot of each chain per trading
day, taken automatically after the close. This is the history the premium-book
backtests (Tom King 1-1-2s, Sosnoff strangles) need and that we'd otherwise
have to buy.

**How it runs.** A GitHub Actions job (`.github/workflows/snapshot.yml`) runs
every 30 minutes each weekday evening, plus a morning catch-up. GitHub can
start scheduled runs hours late, so the schedule is redundant on purpose. It pulls Cboe's free 15-minute-delayed
chains, which carry bid/ask, IV, greeks, open interest and volume, and commits
the results here. It doesn't need a laptop, an API key or Claude.

**What it saves**

| Path | Contents |
|---|---|
| `data/chains/YYYY/YYYY-MM-DD/<SYMBOL>.parquet` | One row per contract, 0–200 DTE, about 1–65Δ: bid, ask, mid, sizes, IV, delta/gamma/vega/theta/rho, OI, volume |
| `data/summary.csv` | One row per symbol per day: spot, ATM IV at 30/60/90 days, 25Δ and 16Δ put/call IV, skew, term slope, put/call volume, 0DTE volume share, and the feed's own fields such as iv30 |

The summary feeds IV rank and IV percentile once a year of history exists.
Until then, Cboe's volatility indices (VIX, VXN, GVZ, OVX, VXEEM) can stand
in where one exists.

**Rules it follows**
- It replaces a day's file only with a better snapshot: post-close beats
  intraday, a later intraday stamp beats an earlier one, and the first close
  saved for a day is kept. Reruns are safe.
- It refuses to replace a file with one under half its size, and rejects a
  chain with fewer than 20 contracts or few live quotes.
- Each snapshot is filed under its trading day: the stamp's own day once that
  session has opened, otherwise the previous weekday. A run delayed past
  midnight still files the close it sees.
- If the chain shows trades after 4:20 PM, Cboe's overnight session (SPX,
  NDX and VIX, from 8:15 PM New York) has moved the quotes, and the snapshot
  is not filed as a close. Those three need a run between 4:30 and 8:15 PM;
  ETF and stock chains can be caught any time before the next open.
- A morning check (7:37 AM CT) fails the run (GitHub emails the owner) if
  any symbol is missing the last session's close. If no symbol has one, the
  day is treated as a market holiday.

**Timing.** Quotes lag the feed's stamp by about 15 minutes, and SPY/QQQ/IWM
options trade until 4:15 PM, so only a stamp from 4:30 PM New York on counts
as a close.

**Proxies.** CME futures options aren't in this feed, so SPX stands in for
/ES, NDX for /NQ, GLD for /GC, SLV for /SI, TLT for /ZB and USO for /CL.
/NG and /6E have no usable proxy here (UNG's and FXE's Cboe feeds are stale),
so they wait for the Tastytrade API, which can also add the real futures
chains.

**VIX caveat.** Cboe's IVs for VIX options look wrong: on day one the ATM IV
came out near 218%, with puts richer than calls. The likely cause is that
they're computed against spot VIX rather than the VIX future each option
settles to. Use VIX option prices, not their IV or delta columns.

**Changing the list.** Edit `symbols.txt`. Index options take Cboe's
underscore prefix (`_SPX`).

**Size.** Expect a few MB per day. Once a year, move the previous year's
`data/chains/` into the collector's Parquet store and delete it here, so the
repo stays small.

## External feeds

`feeds.py` (workflow `.github/workflows/feeds.yml`) runs weekday evenings and
weekday mornings. It pulls from primary sources only and writes CSVs under
`data/external/`.

| Path | Source | History |
|---|---|---|
| `vol/<INDEX>.csv` | Cboe: VIX, VIX9D, VIX3M, VIX6M, VVIX, VXN, RVX, SKEW, GVZ, OVX, VXEEM | VIX and SKEW from 1990; most others from 2009 |
| `cot/{legacy,tff,disagg}/<YEAR>.csv` | CFTC public reporting API, futures only | 2006 on, every market |
| `cot/markets.csv` | contract code → market name and group | |
| `cot/releases.csv` | nominal release (Friday 3:30 PM ET) and when we first saw each report | |
| `fred/<SERIES>.csv` | FRED official API: Moody's BAA/AAA spreads, NASDAQ-100, VXN (2001 on), Treasury yields, real yields, breakevens, NFCI, Fed balance sheet, RRP, TGA, dollar, WTI, claims | back to the 1980s for credit and NASDAQ-100 |
| `bars/<TICKER>.csv` | Yahoo daily OHLCV, dividends, splits: 40 ETFs, indexes, AI names, crypto | full history (QQQ from 1999) |
| `events/macro.csv` | FRED release dates: CPI, jobs, PPI, retail sales, GDP, PCE, JOLTS, claims | 2000 on, plus scheduled |
| `events/fomc.csv` | FOMC decision days (Fed calendar page) | 2021 on, plus scheduled |
| `events/earnings.csv` | earnings dates, EPS estimate/actual, surprise %: 12 AI and mega-cap names | ~2002 on, plus next date |
| `events/events.csv` | all catalysts in one table, with a `scheduled` flag | |
| `_last_run.csv` | status and seconds per feed for the latest run | |

FRED needs a `FRED_API_KEY` repository secret: its public CSV link times out
from GitHub's runners. Without the secret, FRED shows as waiting, not failed.

**Rules.** A row we hold is never dropped, even if the source stops
publishing it. An update that would change more than 2% of held values is
refused unless the series revises itself (NFCI, ANFCI, claims). Git history
is the point-in-time record: the files as of any commit are exactly what we
knew then. Bars keep Yahoo's split-adjusted close (not the dividend-adjusted
one, which Yahoo rewrites on every dividend); closes older than 5 days must
match what's held, and a split reloads that ticker. In the event files, past
rows are never dropped and upcoming rows follow the source, so a rescheduled
release doesn't leave a ghost. Yahoo's EPS surprise can compare GAAP results
to adjusted estimates; check one-off items before calling a quarter a beat
or a miss. The first COT backfill took about 3 minutes. Each run caps
backfill at 10 minutes and stops starting fetches after 20, so a slow source
can't lose a run.

## Crowding monitor

`crowding.py` (workflow `.github/workflows/crowding.yml`, Saturday mornings plus
a Monday catch-up) reads the COT files and ranks 41 core futures by how extreme
positioning is. Output goes to `data/derived/crowding/history.csv` (every market,
every report since 2006) and `latest.csv`, and a summary goes to Telegram once
per new report.

- **COT index:** where net positioning sits in its 3-year (156-report) range,
  0 to 100. Crowded long means 90 or higher; crowded short means 10 or lower.
- **Primary group:** leveraged funds for financials, managed money for
  commodities. Large speculators (legacy) are shown alongside.
- **Price context:** 4-week range and distance from the 20- and 50-day averages
  in ATRs, from Yahoo's continuous futures (Sierra replaces this later). A
  crowded market whose price has crossed its 20-day against the crowd is flagged.
- **Point-in-time:** each report carries `available_at`; `as_of()` only returns
  reports public at a given moment. Tested in `tests/test_crowding.py`.

Crowding is context, not a signal: the trigger is price failing against the
crowd.

## Instrument map

`instruments.py` says how each of the 41 markets gets traded at Robinhood
(Part 2 of an alert). The crowding report's "fade via" lines come from it, and
the weekly job rewrites `data/derived/instruments.csv`.

- **Micro future** when Robinhood lists it and one contract's stop risk fits
  the per-trade budget. The stop is provisionally 2 x daily ATR(14), until the
  backtests give a median stop per market. The minimum book is that dollar
  risk divided by the risk per trade (0.5%).
- **ETF proxy** otherwise: shares for longs; for shorts, puts or put debit
  spreads (weekly chains), or small limit-order put spreads (monthly-only
  chains). Commodity ETFs carry roll drag on multi-week holds. ETF trades go
  in the taxable account.
- **Watch-only** when there's neither: Mexican peso, heating oil, soybean
  oil and meal, coffee, cocoa, cotton, cattle and hogs.
- `rh_status`: confirmed means checked in the Robinhood app on Oct 31, 2026.
  That covers all index, FX, metals, energy and crypto micros in the map.
  Index markets also have E-nano contracts (NES $0.50, NNQ $0.20, N2K $0.50,
  NDOW $0.05 per point; 1/10 of a micro), so index trades fit books 10x
  smaller. They're new (Aug 2026), so check the spread.
- `config/fund.json` holds the swing book size (null until the book-map
  decision) and the risk per trade.

**Tests.** `python tests/test_*.py` (snapshot, crowding, feeds, feeds_more, instruments, notify)
check parsing, filtering, the replace and merge rules, and the IV math on
synthetic data, with no network.

The data is Cboe's delayed feed, kept for personal research.
