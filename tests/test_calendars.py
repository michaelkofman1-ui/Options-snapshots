"""Calendar parsers and schedule rules, offline on snippets shaped like the
real pages. Run: python tests/test_calendars.py"""
import io
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import calendars as K  # noqa: E402


def test_eia():
    html = ('<a href="/petroleum/supply/weekly/archive/2026/2026_01_14/wpsr_2026_01_14.php">x</a>'
            '<a href="/petroleum/supply/weekly/archive/2026/2026_01_22/wpsr_2026_01_22.php">x</a>')
    assert K.parse_wpsr_archive(html) == ["2026-01-14", "2026-01-22"]
    hol = K.federal_holidays()
    # MLK Day (Mon Jan 19, 2026) pushes the petroleum report to Thursday
    assert K.wpsr_rule("2026-01-12", "2026-01-25", hol) == ["2026-01-14", "2026-01-22"]
    # Thanksgiving Thursday pulls the gas storage report to Wednesday
    assert K.wngsr_rule("2026-11-16", "2026-11-30", hol) == ["2026-11-19", "2026-11-25"]


def test_esmis():
    html = ('<title>Cattle on Feed | Economics</title><td headers="x" class="views-field views-field-release-date">'
            '<time datetime="2026-10-24T12:00:00Z">Oct 24 2026</time>')
    assert K.parse_esmis(html) == ("Cattle on Feed | Economics", ["2026-10-24"])


def test_central_banks():
    assert K.parse_ecb('<dt isoDate="2015-12-03"><div class="date">3 December 2015</div></dt>') == ["2015-12-03"]
    assert K.parse_boj('<a href="/en/mopo/mpmdeci/mpr_2015/k150121a.pdf">x</a>'
                       '<a href="/en/mopo/mpmdeci/state_2007/k070118.pdf">y</a>') == ["2015-01-21", "2007-01-18"]
    assert K.parse_rba('/rba-board-minutes/2015/2015-12-01.html /rba-board-minutes/2008/04032008.html') == \
        ["2015-12-01", "2008-03-04"]
    assert K.parse_boc('<th data-date="2026-10-28">October 28, 2026</th>') == ["2026-10-28"]
    assert K.parse_snb("<p>Monetary policy assessment of 10 December 2015</p>") == ["2015-12-10"]
    assert K.parse_banxico('<TD tag="[current].bm:referenceDate" class=bmdateview aria-label="17 de Diciembre 2015">'
                           '<SPAN aria-hidden=true>17/12/15') == ["2015-12-17"]
    buf = io.BytesIO()
    pd.DataFrame({"a": [None, None, "Bank Rate"], "b": ["title", pd.Timestamp("1997-06-06"), pd.Timestamp("2026-11-05")]}) \
        .to_excel(buf, sheet_name="Bank Rate Decisions", header=False, index=False)
    assert K.parse_boe(buf.getvalue()) == ["1997-06-06", "2026-11-05"]


def test_boc_releases():
    def art(day, title, excerpt):
        return (f"<article class=\"media\"><span class='media-date pull-right'>{day}</span>"
                f"<h3 class=\"media-heading\"><a href=\"x\">{title}</a></h3>"
                f"<div class='media-excerpt'>{excerpt}</div></article>")
    html = (art("October 28, 2026", "Bank of Canada raises the policy rate by 25 basis points to 2½%", "The Bank today...")
            + art("September 3, 2026", "Bank of Canada unveils new vertical $20 bank note", "Her Excellency...")
            + art("January 20, 2009", "Bank of Canada lowers overnight rate target by 1/2 percentage point to 1 per cent", "")
            + art("June 10, 2008", "Statement", "The Bank of Canada today announced that it is maintaining its target for the overnight rate at 3 per cent."))
    assert K.parse_boc_releases(html) == ["2026-10-28", "2009-01-20", "2008-06-10"]
    held = art("March 10, 2021", "Bank of Canada will maintain current level of policy rate until inflation objective is sustainably achieved",
               "The Bank of Canada today held its target for the overnight rate at the effective lower bound of ¼ percent.")
    assert K.parse_boc_releases(held) == ["2021-03-10"]


def test_fomc_history():
    html = ("<h5>January 29-30 Meeting - 2008</h5><h5>January 21 Conference Call</h5>"
            "<h5>April/May 30-1 Meeting - 2013</h5><h5>October 7 (unscheduled) Meeting - 2008</h5>")
    assert K.parse_fomc_history(html, 2008) == ["2008-01-30", "2008-05-01", "2008-10-07"]


def test_frame_drops_old_and_dupes():
    f = K.frame(["2005-12-01", "2026-01-02", "2026-01-02"], "ECB", "08:15")
    assert list(f["date"]) == ["2026-01-02"] and list(f.columns) == ["date", "time_et", "event", "detail", "basis"]


if __name__ == "__main__":
    test_eia()
    test_esmis()
    test_central_banks()
    test_boc_releases()
    test_fomc_history()
    test_frame_drops_old_and_dupes()
    print("all checks passed")
