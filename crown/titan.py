"""Titan007 Crown company-id 3 adapter.  Crown is never used as PinnAPI data."""
from __future__ import annotations

import html
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any

from .common import HKT
from .config import Settings
from .lines import parse_titan_handicap, parse_titan_total

_ROW = re.compile(
    r"<tr\b(?P<attrs_before>[^>]*)\bsId=['\"](?P<id>\d+)['\"]"
    r"(?P<attrs_after>[^>]*)>(?P<body>[\s\S]*?)</tr>",
    re.I,
)
_TAG = re.compile(r"<[^>]*>")
_TEAM_TV_STATS = re.compile(
    r"(?:var\s+)?teamTvStatisticData\s*=\s*['\"](?P<data>[^'\"]*)",
    re.I,
)


def _text(value: str) -> str:
    return html.unescape(_TAG.sub("", value)).replace("\xa0", " ").strip()


def parse_titan_time(raw: str, yyyymmdd: str) -> datetime | None:
    first = re.search(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", raw)
    second = re.search(r"(\d{1,2})日\s*(\d{1,2}):(\d{2})", raw)
    try:
        year, page_month = int(yyyymmdd[:4]), int(yyyymmdd[4:6])
        if first:
            month, day, hour, minute = map(int, first.groups())
        elif second:
            day, hour, minute = map(int, second.groups())
            month = page_month
        else:
            return None
        return datetime(year, month, day, hour, minute, tzinfo=HKT)
    except ValueError:
        return None


def parse_schedule_page(source: str, yyyymmdd: str) -> list[dict[str, Any]]:
    fixtures = []
    for match in _ROW.finditer(source):
        # The schedule page's initial CSS visibility is only its default
        # league view. Selecting Crown in the page reveals additional rows
        # that also carry valid company-id 3 prices, so fixture discovery
        # must include both visible and initially hidden rows. The dashboard
        # still requires a successfully parsed Crown quote before showing one.
        cells = [_text(cell) for cell in re.findall(r"<td[^>]*>[\s\S]*?</td>", match.group("body"), re.I)]
        if len(cells) < 7:
            continue
        kickoff = parse_titan_time(cells[1], yyyymmdd)
        if not kickoff:
            continue
        score = re.search(r"(\d+)\s*-\s*(\d+)", cells[4])
        fixtures.append({"id": match.group("id"), "league": re.sub(r"\[\d+\]$", "", cells[0]).strip(),
                         "kickoff": kickoff, "status": cells[2], "home": re.sub(r"\[[^\]]*]", "", cells[3]).strip(),
                         "away": re.sub(r"\[[^\]]*]", "", cells[5]).strip(),
                         "home_score": int(score.group(1)) if score else None,
                         "away_score": int(score.group(2)) if score else None})
    return fixtures


def parse_match_statistics(source: str) -> dict[str, int] | None:
    """Parse full-time team statistics embedded in a Titan live-detail page.

    Titan encodes rows as ``stat_code,home,away,home_pct,away_pct`` separated
    by ``^``.  Verified stat code 0 is the full-time corner count.  Returning
    ``None`` is intentionally fail-closed when the field is absent or malformed.
    """
    found = _TEAM_TV_STATS.search(source)
    if not found:
        return None
    for raw_row in found.group("data").split("^"):
        fields = [field.strip() for field in raw_row.split(",")]
        if len(fields) < 3 or fields[0] != "0":
            continue
        try:
            home, away = int(fields[1]), int(fields[2])
        except ValueError:
            return None
        if home < 0 or away < 0:
            return None
        return {
            "corners_home": home,
            "corners_away": away,
            "corners_total": home + away,
        }
    return None


def parse_crown_fixture_ids(source: str) -> set[str]:
    """Return the exact fixture IDs exposed by Titan's company-id feed."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return set()
    ids_node = root.find("ids")
    ids = {
        value.strip()
        for value in (ids_node.text or "").split(",")
        if value.strip()
    } if ids_node is not None else set()
    if ids:
        return ids
    return {
        fields[0]
        for node in root.findall("./match/m")
        if node.text and (fields := node.text.split(",")) and fields[0]
    }


def _row_triple(row: str) -> tuple[float, float, float] | None:
    # `wholeOdds` is the visible current quote.  Titan can leave malformed or
    # stale `wholeLastOdds` cells hidden in the same row, so never prefer a
    # display:none snapshot over the visible market.
    for kind in ("wholeOdds", "wholeLastOdds"):
        cells = [
            cell for cell in re.findall(
                rf"<td[^>]*oddstype=['\"]{kind}['\"][^>]*>[\s\S]*?</td>",
                row,
                re.I,
            )
            if not re.search(r"display\s*:\s*none", cell, re.I)
        ]
        if len(cells) < 3:
            continue
        try:
            home = float(_text(cells[0]))
            goals = float(re.search(r"goals=['\"](-?[\d.]+)", cells[1], re.I).group(1))  # type: ignore[union-attr]
            away = float(_text(cells[2]))
        except (AttributeError, ValueError):
            continue
        if home > 0 and away > 0:
            return home, goals, away
    return None


def parse_crown_asian(source: str, company_id: str = "3") -> tuple[float, float, float] | None:
    """Only the configured Crown company ID is trusted; masked names are not a fallback."""
    for row in re.findall(r"<tr[^>]*>[\s\S]*?</tr>", source, re.I):
        found = re.search(r'data-id=["\'](\d+)["\']|companyID=["\'](\d+)["\']', row, re.I)
        if found and (found.group(1) or found.group(2)) == company_id:
            return _row_triple(row)
    return None


def crown_prices_from_pages(asian_html: str | None, total_html: str | None, company_id: str = "3",
                            observed_at: float | None = None) -> list[dict[str, Any]]:
    observed_at = observed_at or time.time()
    prices: list[dict[str, Any]] = []
    if asian_html:
        row = parse_crown_asian(asian_html, company_id)
        line = parse_titan_handicap(row[1]) if row else None
        if row and line is not None:
            prices += [{"market": "HDC", "line": line, "selection": "H", "odds": row[0] + 1, "source_at": observed_at},
                       {"market": "HDC", "line": line, "selection": "A", "odds": row[2] + 1, "source_at": observed_at}]
    if total_html:
        row = parse_crown_asian(total_html, company_id)
        line = parse_titan_total(row[1]) if row else None
        if row and line is not None:
            prices += [{"market": "HIL", "line": line, "selection": "H", "odds": row[0] + 1, "source_at": observed_at},
                       {"market": "HIL", "line": line, "selection": "L", "odds": row[2] + 1, "source_at": observed_at}]
    return prices


class TitanClient:
    def __init__(self, config: Settings):
        self.config = config

    @staticmethod
    def _read(url: str) -> str:
        last_error: OSError | None = None
        for attempt in range(2):
            is_vip_odds = "vip.titan007.com/" in url
            is_live_static = "livestatic.titan007.com/" in url
            request = urllib.request.Request(
                url,
                headers={
                    # Titan's VIP odds host returns HTTP 442 to generic bot
                    # user agents.  These are ordinary browser navigation
                    # headers; no cookie, login, or anti-bot bypass is used.
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/127.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    **(
                        {"Referer": "https://live.titan007.com/index2in1.aspx?id=3"}
                        if is_live_static
                        else {"Referer": "http://bf.titan007.com/football/"}
                        if is_vip_odds
                        else {}
                    ),
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    return response.read().decode("gb18030", errors="replace")
            except OSError as exc:
                last_error = exc
                if attempt < 1:
                    time.sleep(1.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def fixtures(self, offsets: tuple[int, ...] = (0, 1)) -> list[dict[str, Any]]:
        try:
            company_feed = self._read(
                "https://livestatic.titan007.com/vbsxml/"
                f"goal{self.config.titan_company_id}.xml?r=007{int(time.time() * 1000)}"
            )
            crown_ids = parse_crown_fixture_ids(company_feed)
        except OSError:
            crown_ids = set()
        if not crown_ids:
            return []
        output: list[dict[str, Any]] = []
        for offset in offsets:
            day = (datetime.now(HKT) + timedelta(days=offset)).strftime("%Y%m%d")
            try:
                # `Over_YYYYMMDD` is a completed-results page.  Falling back to
                # it after a transient `Next` timeout silently removes all
                # upcoming matches, so fixture discovery must use Next only.
                output.extend(
                    row
                    for row in parse_schedule_page(
                        self._read(f"{self.config.titan_bf_base}/Next_{day}.htm"),
                        day,
                    )
                    if str(row["id"]) in crown_ids
                )
            except OSError:
                continue
        return list({row["id"]: row for row in output}.values())

    def crown_prices(self, titan_id: str) -> list[dict[str, Any]]:
        return self.crown_price_snapshot(titan_id)["prices"]

    def crown_price_snapshot(self, titan_id: str) -> dict[str, Any]:
        """Return prices plus per-market fetch status.

        An empty successful page means Crown no longer has that market.  A
        network failure is different: callers must keep the last known market
        rather than making the whole fixture disappear from the dashboard.
        """
        asian = total = None
        asian_ok = total_ok = False
        try:
            asian = self._read(f"{self.config.titan_vip_base}/AsianOdds_n.aspx?id={titan_id}")
            asian_ok = True
        except OSError:
            pass
        try:
            total = self._read(f"{self.config.titan_vip_base}/OverDown_n.aspx?id={titan_id}")
            total_ok = True
        except OSError:
            pass
        return {
            "prices": crown_prices_from_pages(asian, total, self.config.titan_company_id),
            "asian_ok": asian_ok,
            "total_ok": total_ok,
        }

    def results(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for offset in (0, -1, -2):
            day = (datetime.now(HKT) + timedelta(days=offset)).strftime("%Y%m%d")
            try:
                output.extend(parse_schedule_page(self._read(f"{self.config.titan_bf_base}/Over_{day}.htm"), day))
            except OSError:
                continue
        return list({row["id"]: row for row in output}.values())

    def result_detail(self, titan_id: str) -> dict[str, Any] | None:
        """Return machine-readable full-time statistics for one numeric ID."""
        titan_id = str(titan_id)
        if not titan_id.isdigit():
            return None
        stats = parse_match_statistics(
            self._read(f"https://live.titan007.com/detail/{titan_id}cn.htm")
        )
        if not stats:
            return None
        return {
            "titan_id": titan_id,
            **stats,
            "source": "titan007_match_detail",
        }
