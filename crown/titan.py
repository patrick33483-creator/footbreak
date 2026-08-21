"""Titan007 Crown company-id 3 adapter.  Crown is never used as PinnAPI data."""
from __future__ import annotations

import html
import multiprocessing
import os
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


def _bounded_read_process(
    send: Any,
    url: str,
    encoding: str,
    timeout: float,
    attempts: int,
) -> None:
    """Run one untrusted provider read outside the deadline owner process."""
    try:
        send.send((
            "ok",
            TitanClient._read_direct(
                url, encoding=encoding, timeout=timeout, attempts=attempts,
            ),
        ))
    except BaseException as exc:
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


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


def parse_match_header(source: str, titan_id: str) -> dict[str, Any] | None:
    """Parse Titan's stable analysis-header record for one exact fixture ID.

    Fields 4, 10 and 11 are respectively match state, home score and away
    score in the same record used by Titan's live detail page.  Only a
    completed match (state -1) with a numeric score is accepted.
    """
    fields = source.strip().split("^")
    if len(fields) < 16 or fields[4].strip() != "-1":
        return None
    try:
        home_score = int(fields[10])
        away_score = int(fields[11])
        kickoff = datetime.strptime(fields[5], "%Y%m%d%H%M%S").replace(tzinfo=HKT)
    except (ValueError, IndexError):
        return None
    if home_score < 0 or away_score < 0:
        return None
    return {
        "id": str(titan_id),
        "home": fields[0].strip(),
        "away": fields[1].strip(),
        "league": fields[15].strip(),
        "kickoff": kickoff,
        "status": "完",
        "home_score": home_score,
        "away_score": away_score,
    }


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


def parse_crown_bulk_prices(
    source: str,
    observed_at: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Parse exact current Crown-ID-3 HDC/HIL rows from Titan's bulk feed.

    The company-specific ``goal3.xml`` feed carries one CSV record per exact
    Titan fixture.  Only its documented current handicap/total fields are
    accepted; malformed rows, invalid Asian lines, and non-positive Hong Kong
    water fail closed.  The feed does not expose a dependable live-state field,
    so callers must additionally require a pre-kickoff fixture before use.
    """
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return {}
    observed_at = observed_at or time.time()
    output: dict[str, dict[str, Any]] = {}
    for node in root.findall("./match/m"):
        if not node.text:
            continue
        fields = [value.strip() for value in node.text.split(",")]
        if len(fields) < 13 or not fields[0]:
            continue
        match_id = fields[0]
        prices: list[dict[str, Any]] = []
        try:
            handicap = parse_titan_handicap(float(fields[2]))
            home_water, away_water = float(fields[3]), float(fields[4])
        except (TypeError, ValueError):
            handicap = None
            home_water = away_water = 0.0
        if (
            handicap is not None
            and home_water > 0
            and away_water > 0
            and home_water + 1 > 1
            and away_water + 1 > 1
        ):
            prices.extend([
                {
                    "market": "HDC", "line": handicap, "selection": "H",
                    "odds": home_water + 1, "source_at": observed_at,
                },
                {
                    "market": "HDC", "line": handicap, "selection": "A",
                    "odds": away_water + 1, "source_at": observed_at,
                },
            ])
        try:
            total = parse_titan_total(float(fields[10]))
            over_water, under_water = float(fields[11]), float(fields[12])
        except (TypeError, ValueError):
            total = None
            over_water = under_water = 0.0
        if (
            total is not None
            and over_water > 0
            and under_water > 0
            and over_water + 1 > 1
            and under_water + 1 > 1
        ):
            prices.extend([
                {
                    "market": "HIL", "line": total, "selection": "H",
                    "odds": over_water + 1, "source_at": observed_at,
                },
                {
                    "market": "HIL", "line": total, "selection": "L",
                    "odds": under_water + 1, "source_at": observed_at,
                },
            ])
        if prices:
            output[match_id] = {
                "prices": prices,
                "asian_ok": any(row["market"] == "HDC" for row in prices),
                "total_ok": any(row["market"] == "HIL" for row in prices),
                "quote_source": "titan007-crown-id-3-bulk-current",
                "company_id": "3",
                "bulk_observed_at": observed_at,
            }
    return output


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
        self._result_detail_cache: dict[str, dict[str, Any] | None] = {}
        self._result_detail_requests_remaining: int | None = None

    def limit_result_detail_requests(self, limit: int) -> None:
        """Bound exact-detail fanout for one caller/pass.

        Missing rows remain pending for the next scheduled pass.  The limit is
        deliberately per client instance, so a large historical recovery set
        cannot monopolize the settlement service.
        """
        self._result_detail_requests_remaining = max(0, int(limit))

    @staticmethod
    def _read_direct(
        url: str,
        encoding: str = "gb18030",
        *,
        timeout: float = 25,
        attempts: int = 2,
    ) -> str:
        """Read a Titan endpoint with caller-selectable bounded retries.

        Existing page/discovery callers retain the established 25-second,
        two-attempt policy.  The deadline-bound Crown bulk path supplies a
        narrower policy so a static-host TLS failure cannot consume a tick.
        """
        attempts = max(1, int(attempts))
        last_error: OSError | None = None
        for attempt in range(attempts):
            is_vip_odds = "vip.titan007.com/" in url
            is_live_static = "livestatic.titan007.com/" in url
            headers = {
                # Titan's VIP odds host returns HTTP 442 to generic bot user
                # agents.  These are ordinary browser navigation headers; no
                # cookie, login, or anti-bot bypass is used.
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
            }
            if is_live_static:
                # The static XML host intermittently closes default compressed
                # keep-alive requests during TLS negotiation.  Keep this to
                # benign browser-compatible request semantics, scoped only to
                # that host, so VIP/page requests retain their old behavior.
                headers.update({
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
            request = urllib.request.Request(
                url,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read().decode(encoding, errors="replace")
            except OSError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _read(
        url: str,
        encoding: str = "gb18030",
        *,
        timeout: float = 25,
        attempts: int = 2,
        hard_deadline: float | None = None,
    ) -> str:
        """Read Titan directly, or kill an uncooperative read at a hard deadline.

        urllib's timeout does not cover every DNS/TLS/library stall.  Tick and
        settlement callers supply ``hard_deadline`` so that a stuck provider
        can never hold their owning process past its wall-clock budget.
        """
        if hard_deadline is None or os.name != "posix":
            return TitanClient._read_direct(
                url, encoding=encoding, timeout=timeout, attempts=attempts,
            )
        remaining = max(0.0, hard_deadline)
        if remaining <= 0:
            raise TimeoutError("titan_read_deadline_exhausted")
        context = multiprocessing.get_context("fork")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_bounded_read_process,
            args=(sender, url, encoding, timeout, attempts),
        )
        process.start()
        sender.close()
        try:
            if not receiver.poll(remaining):
                raise TimeoutError("titan_read_deadline_exhausted")
            status, value = receiver.recv()
            if status == "ok" and isinstance(value, str):
                return value
            raise OSError(f"titan_read_{value}")
        except EOFError as exc:
            raise OSError("titan_read_worker_exited") from exc
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.03)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.03)

    def fixtures(self, offsets: tuple[int, ...] = (0, 1)) -> list[dict[str, Any]]:
        try:
            source = self._read(
                "https://livestatic.titan007.com/vbsxml/"
                f"goal{self.config.titan_company_id}.xml?r=007{int(time.time() * 1000)}"
            )
            bulk = (
                parse_crown_bulk_prices(source, observed_at=time.time())
                if self.config.titan_company_id == "3" else {}
            )
            # Discovery retains every exact company-feed ID, including a row
            # whose current bulk quote is malformed.  Such a fixture can use
            # the slower page fallback, but a valid bulk row never needs it.
            crown_ids = parse_crown_fixture_ids(source)
            self._last_crown_bulk_snapshots = bulk
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

    def crown_bulk_price_snapshots(
        self, *, max_seconds: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch company-ID-3 bulk current odds once for an entire tick."""
        if self.config.titan_company_id != "3":
            return {}
        source = self._read(
            "https://livestatic.titan007.com/vbsxml/"
            f"goal{self.config.titan_company_id}.xml?r=007{int(time.time() * 1000)}",
            timeout=8.0 if max_seconds is None else min(8.0, max(0.1, max_seconds)),
            attempts=1,
            hard_deadline=max_seconds,
        )
        return parse_crown_bulk_prices(source, observed_at=time.time())

    def crown_prices(self, titan_id: str) -> list[dict[str, Any]]:
        return self.crown_price_snapshot(titan_id)["prices"]

    def crown_price_snapshot(
        self, titan_id: str, *, max_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Return prices plus per-market fetch status.

        An empty successful page means Crown no longer has that market.  A
        network failure is different: callers must keep the last known market
        rather than making the whole fixture disappear from the dashboard.
        """
        # This adapter is intentionally a Crown company-ID-3 adapter.  Do not
        # silently turn an environment typo into a different bookmaker's
        # evidence under Crown provenance.
        if self.config.titan_company_id != "3":
            return {"prices": [], "asian_ok": False, "total_ok": False}
        asian = total = None
        asian_ok = total_ok = False
        # The T-5 fallback supplies a hard per-fixture budget.  Split it
        # across the independent handicap and total pages so either stalled
        # page cannot consume the whole tick.  Existing callers keep the
        # established direct-read behaviour when no budget is supplied.
        page_budget = (
            max(0.1, float(max_seconds) / 2.0)
            if max_seconds is not None else None
        )
        try:
            asian = self._read(
                f"{self.config.titan_vip_base}/AsianOdds_n.aspx?id={titan_id}",
                timeout=min(8.0, page_budget) if page_budget is not None else 25.0,
                attempts=1 if page_budget is not None else 2,
                hard_deadline=page_budget,
            )
            asian_ok = True
        except OSError:
            pass
        try:
            total = self._read(
                f"{self.config.titan_vip_base}/OverDown_n.aspx?id={titan_id}",
                timeout=min(8.0, page_budget) if page_budget is not None else 25.0,
                attempts=1 if page_budget is not None else 2,
                hard_deadline=page_budget,
            )
            total_ok = True
        except OSError:
            pass
        return {
            "prices": crown_prices_from_pages(asian, total, self.config.titan_company_id),
            "asian_ok": asian_ok,
            "total_ok": total_ok,
            "quote_source": "titan007-crown-id-3",
        }

    def results(
        self,
        dates: set[str] | None = None,
        *,
        max_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return completed results for the requested HKT calendar dates.

        Settlement callers pass every still-pending kickoff date.  The old
        fixed three-day window made older missed rows impossible to recover.
        """
        output: list[dict[str, Any]] = []
        days = {
            str(value).replace("-", "")
            for value in (dates or set())
            if str(value).replace("-", "").isdigit()
            and len(str(value).replace("-", "")) == 8
        }
        if not days:
            days = {
                (datetime.now(HKT) + timedelta(days=offset)).strftime("%Y%m%d")
                for offset in (0, -1, -2)
            }
        deadline = time.monotonic() + max_seconds if max_seconds is not None else None
        for day in sorted(days):
            remaining = deadline - time.monotonic() if deadline is not None else 8.0
            if remaining <= 0:
                break
            try:
                output.extend(parse_schedule_page(
                    self._read(
                        f"{self.config.titan_bf_base}/Over_{day}.htm",
                        timeout=max(0.1, min(8.0, remaining)),
                        attempts=1,
                        hard_deadline=remaining if max_seconds is not None else None,
                    ),
                    day,
                ))
            except OSError:
                continue
        return list({row["id"]: row for row in output}.values())

    def result_detail(
        self, titan_id: str, *, max_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Return an exact-ID completed score and available full-time stats."""
        titan_id = str(titan_id)
        if not titan_id.isdigit():
            return None
        if titan_id in self._result_detail_cache:
            return self._result_detail_cache[titan_id]
        if self._result_detail_requests_remaining is not None:
            if self._result_detail_requests_remaining <= 0:
                return None
            self._result_detail_requests_remaining -= 1
        deadline = time.monotonic() + max_seconds if max_seconds is not None else None

        def remaining() -> float:
            return (
                deadline - time.monotonic()
                if deadline is not None else 8.0
            )

        header_budget = remaining()
        if header_budget <= 0:
            return None
        header = parse_match_header(
            self._read(
                "https://livestatic.titan007.com/phone/txt/analysisheader/cn/"
                f"{titan_id[0]}/{titan_id[1:3]}/{titan_id}.txt",
                encoding="utf-8",
                timeout=max(0.1, min(8.0, header_budget)),
                attempts=1,
                hard_deadline=header_budget if max_seconds is not None else None,
            ),
            titan_id,
        )
        if not header:
            self._result_detail_cache[titan_id] = None
            return None
        stats_budget = remaining()
        if stats_budget <= 0:
            self._result_detail_cache[titan_id] = None
            return None
        stats = parse_match_statistics(
            self._read(
                f"https://live.titan007.com/detail/{titan_id}cn.htm",
                timeout=max(0.1, min(8.0, stats_budget)),
                attempts=1,
                hard_deadline=stats_budget if max_seconds is not None else None,
            )
        ) or {}
        result = {
            **header,
            **stats,
            "source": "titan007_match_detail",
        }
        self._result_detail_cache[titan_id] = result
        return result
