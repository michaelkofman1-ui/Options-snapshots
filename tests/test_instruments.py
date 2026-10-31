#!/usr/bin/env python3
"""Offline checks for the instrument map. Run: python tests/test_instruments.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import instruments as I  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp()) / "latest.csv"
    I.pd.DataFrame({"symbol": ["NQ", "6E", "ES"], "close": [28543, 1.118, 7614],
                    "atr14": [680.0, 0.006, 94.0]}).to_csv(tmp, index=False)
    t = I.table(0.5, tmp)
    nq = t.set_index("symbol").loc["NQ"]
    assert nq["stop_usd_per_micro"] == 2720 and nq["min_book_usd"] == 544000   # 2 x 680 x $2
    assert I.expression("NQ", "short", 25_000, tab=t).startswith("QQQ puts")
    assert I.expression("NQ", "short", 600_000, tab=t).startswith("MNQ micro future (short)")
    assert I.expression("6E", "long", 50_000, tab=t).startswith("M6E")           # $150 risk fits
    assert I.expression("6J", "long", 1e9, tab=t).startswith("FXY")               # unverified listing
    assert I.expression("ZB", "short", 1e9, tab=t).startswith("TLT puts")
    assert I.expression("SB", "short", None, tab=t).startswith("CANE small put debit")
    assert I.expression("HE", "long", None, tab=t) == "watch-only: no ETF"
    assert set(I.MAP) >= {"ES", "NQ", "CL", "GC", "HE"} and len(I.MAP) == 41
    print("all checks passed")


if __name__ == "__main__":
    main()
