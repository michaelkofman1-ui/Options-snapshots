"""Development helper: fetch candidate source pages and save the raw bytes, so
parsers can be written against what the cloud actually receives. Writes
probe/<n>.<ext> and probe/index.csv. Not part of any data path.

    python tools/probe.py urls.txt
"""
import csv
import sys
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def main(listfile):
    out = Path("probe")
    out.mkdir(exist_ok=True)
    rows = []
    for n, url in enumerate(u.strip() for u in Path(listfile).read_text().splitlines()):
        if not url or url.startswith("#"):
            continue
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body, status, ctype = r.read(), r.status, r.headers.get("Content-Type", "")
        except Exception as e:
            body, status, ctype = str(e).encode(), getattr(e, "code", "ERR"), ""
        ext = "xlsx" if "spreadsheet" in ctype or url.endswith(".xlsx") else "json" if "json" in ctype else \
            "xml" if "xml" in ctype else "html"
        (out / f"{n:02d}.{ext}").write_bytes(body[:3_000_000])
        rows.append({"n": n, "status": status, "bytes": len(body), "type": ctype, "url": url})
        print(n, status, len(body), url, flush=True)
    with open(out / "index.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["n", "status", "bytes", "type", "url"])
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
