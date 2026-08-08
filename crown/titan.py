"""Titan007 Crown company-id 3 adapter.  Crown is never used as PinnAPI data."""
from __future__ import annotations

import html
import re
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from .common import HKT
from .config import Settings
from .lines import parse_titan_handicap, parse_titan_total

_ROW = re.compile(r"<tr[^>]*sId=['\"](\d+)['\"][^>]*>([\s\S]*?)</tr>", re.I)
_TAG = re.compile(r"<[^>]*>")


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
        cells = [_text(cell) for cell in re.findall(r"<td[^>]*>[\s\S]*?</td>", match.group(2), re.I)]
        if len(cells) < 7:
            continue
        kickoff = parse_titan_time(cells[1], yyyymmdd)
        if not kickoff:
            continue
        score = re.search(r"(\d+)\s*-\s*(\d+)", cells[4])
        fixtures.append({"id": match.group(1), "league": re.sub(r"\[\d+\]$", "", cells[0]).strip(),
                         "kickoff": kickoff, "status": cells[2], "home": re.sub(r"\[[^\]]*]", "", cells[3]).strip(),
                         "away": re.sub(r"\[[^\]]*]", "", cells[5]).strip(),
                         "home_score": int(score.group(1)) if score else None,
                         "away_score": int(score.group(2)) if score else None})
    return fixtures


def _row_triple(row: str) -> tuple[float, float, float] | None:
    for kind in ("wholeLastOdds", "wholeOdds"):
        cells = re.findall(rf"<td[^>]*oddstype=['\"]{kind}['\"][^>]*>[\s\S]*?</td>", row, re.I)
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
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Cache-Control": "no-cache",
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
        output: list[dict[str, Any]] = []
        for offset in offsets:
            day = (datetime.now(HKT) + timedelta(days=offset)).strftime("%Y%m%d")
            try:
                # `Over_YYYYMMDD` is a completed-results page.  Falling back to
                # it after a transient `Next` timeout silently removes all
                # upcoming matches, so fixture discovery must use Next only.
                output.extend(
                    parse_schedule_page(
                        self._read(f"{self.config.titan_bf_base}/Next_{day}.htm"),
                        day,
                    )
                )
            except OSError:
                continue
        return list({row["id"]: row for row in output}.values())

    def crown_prices(self, titan_id: str) -> list[dict[str, Any]]:
        asian = total = None
        try:
            asian = self._read(f"{self.config.titan_vip_base}/AsianOdds_n.aspx?id={titan_id}")
        except OSError:
            pass
        try:
            total = self._read(f"{self.config.titan_vip_base}/OverDown_n.aspx?id={titan_id}")
        except OSError:
            pass
        return crown_prices_from_pages(asian, total, self.config.titan_company_id)

    def results(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for offset in (0, -1, -2):
            day = (datetime.now(HKT) + timedelta(days=offset)).strftime("%Y%m%d")
            try:
                output.extend(parse_schedule_page(self._read(f"{self.config.titan_bf_base}/Over_{day}.htm"), day))
            except OSError:
                continue
        return list({row["id"]: row for row in output}.values())
