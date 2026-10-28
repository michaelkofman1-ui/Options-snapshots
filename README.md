# options-snapshots

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

**Proxies.** CME futures options aren't in this feed, so SPX stands in for
/ES, NDX for /NQ, GLD for /GC, SLV for /SI, TLT for /ZB, USO for /CL, UNG for
/NG and FXE for /6E. When the Tastytrade account exists, its API can add the
real futures chains.

**Changing the list.** Edit `symbols.txt`. Index options take Cboe's
underscore prefix (`_SPX`).

**Size.** Expect a few MB per day. Once a year, move the previous year's
`data/chains/` into the collector's Parquet store and delete it here, so the
repo stays small.

**Tests.** `python tests/test_snapshot.py` checks parsing, filtering, the
replace rules and the IV math on a synthetic chain, with no network.

The data is Cboe's delayed feed, kept for personal research.
