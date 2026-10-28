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
three times each weekday evening. It pulls Cboe's free 15-minute-delayed
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
  intraday, and a later stamp beats an earlier one. Reruns are safe.
- It refuses to replace a file with one under half its size, and rejects a
  chain with fewer than 20 contracts or few live quotes.
- Stamps from Cboe's overnight session (from 8:15 PM New York) are never
  filed as a day's close.
- The last pass each night fails the run (GitHub emails the owner) if any
  symbol is missing that day's close. If no symbol has a close, the day is
  treated as a market holiday.

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
| `_last_run.csv` | status and seconds per feed for the latest run | |

FRED needs a `FRED_API_KEY` repository secret: its public CSV link times out
from GitHub's runners. Without the secret, FRED shows as waiting, not failed.

**Rules.** A row we hold is never dropped, even if the source stops
publishing it. An update that would change more than 2% of held values is
refused unless the series revises itself (NFCI, ANFCI, claims). Git history
is the point-in-time record: the files as of any commit are exactly what we
knew then. The first COT backfill took about 3 minutes. Each run caps
backfill at 10 minutes and stops starting fetches after 20, so a slow source
can't lose a run.

**Tests.** `python tests/test_snapshot.py` and `python tests/test_feeds.py`
check parsing, filtering, the replace and merge rules, and the IV math on
synthetic data, with no network.

The data is Cboe's delayed feed, kept for personal research.
