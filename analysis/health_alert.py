"""Fail-closed data-health alert for the 15-minute reconciliation service.

This module reads only the two public aggregate reports.  It does not open a
learning database and it does not modify prediction, settlement, or dashboard
state.  When an anomaly is found it uses Telegram's HTTPS Bot API directly
with the long-lived bot credentials already provisioned on the server.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.request


HKT = timezone(timedelta(hours=8))
SYSTEMS = ("footbreak", "crown")
SYSTEM_LABELS = {"footbreak": "足破", "crown": "皇冠"}
SETTLE_RETRY_URL = (
    "https://github.com/patrick33483-creator/footbreak/actions/workflows/settle.yml"
)
AUDIT_RETRY_URL = (
    "https://github.com/patrick33483-creator/footbreak/actions/workflows/audit-results.yml"
)


class ReportMalformedError(ValueError):
    """The report cannot safely be interpreted as a data-health artifact."""


@dataclass(frozen=True)
class HealthMetrics:
    duplicate_stage_keys: int
    corner_coverage: float | None
    stale_missing_corner_fixtures: int
    unresolved_fixtures: int
    stale_unresolved_fixtures: int


@dataclass(frozen=True)
class SystemHealth:
    system: str
    metrics: HealthMetrics | None
    issues: tuple[str, ...] = ()
    malformed_reason: str | None = None

    @property
    def abnormal(self) -> bool:
        return bool(self.issues or self.malformed_reason)


@dataclass(frozen=True)
class HealthEvaluation:
    systems: tuple[SystemHealth, ...]
    generation_failed: bool = False

    @property
    def abnormal(self) -> bool:
        return self.generation_failed or any(system.abnormal for system in self.systems)


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReportMalformedError(f"{field_name}唔係物件")
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportMalformedError(f"{field_name}唔係非負整數")
    return value


def _coverage(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportMalformedError("corner_result.coverage唔係數字")
    coverage = float(value)
    if not math.isfinite(coverage) or not 0.0 <= coverage <= 1.0:
        raise ReportMalformedError("corner_result.coverage唔喺0至1")
    return coverage


def parse_report(system: str, payload: Any) -> HealthMetrics:
    """Validate and reduce one public report to the five alert metrics."""
    report = _require_mapping(payload, "report")
    if report.get("report") != "data_health":
        raise ReportMalformedError("report唔係data_health")
    if report.get("system") != system:
        raise ReportMalformedError("system唔相符")
    completeness = _require_mapping(report.get("completeness"), "completeness")
    overall = _require_mapping(completeness.get("overall"), "completeness.overall")
    result = _require_mapping(overall.get("result"), "completeness.overall.result")
    corner = _require_mapping(
        overall.get("corner_result"), "completeness.overall.corner_result"
    )
    settle_due_fixtures = _nonnegative_int(
        result.get("settle_due_fixtures"), "result.settle_due_fixtures"
    )
    fixtures_with_result = _nonnegative_int(
        result.get("fixtures_with_result"), "result.fixtures_with_result"
    )
    if fixtures_with_result > settle_due_fixtures:
        raise ReportMalformedError(
            "result.fixtures_with_result大過result.settle_due_fixtures"
        )
    unresolved_fixtures = max(0, settle_due_fixtures - fixtures_with_result)
    stale_unresolved_fixtures = _nonnegative_int(
        result.get("stale_unresolved_fixtures"),
        "result.stale_unresolved_fixtures",
    )
    if stale_unresolved_fixtures > unresolved_fixtures:
        raise ReportMalformedError(
            "result.stale_unresolved_fixtures大過未解總數"
        )
    return HealthMetrics(
        duplicate_stage_keys=_nonnegative_int(
            overall.get("duplicate_stage_keys"), "duplicate_stage_keys"
        ),
        corner_coverage=_coverage(corner.get("coverage")),
        stale_missing_corner_fixtures=_nonnegative_int(
            corner.get("stale_beyond_retry_fixtures"),
            "corner_result.stale_beyond_retry_fixtures",
        ),
        unresolved_fixtures=unresolved_fixtures,
        stale_unresolved_fixtures=stale_unresolved_fixtures,
    )


def evaluate_report(system: str, path: Path) -> SystemHealth:
    """Read one report and return every alert condition without raising."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        metrics = parse_report(system, payload)
    except FileNotFoundError:
        return SystemHealth(system, None, malformed_reason="報告缺失")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return SystemHealth(system, None, malformed_reason=f"報告讀取/格式錯誤:{type(exc).__name__}")
    except ReportMalformedError as exc:
        return SystemHealth(system, None, malformed_reason=f"報告格式錯誤:{exc}")

    issues: list[str] = []
    if metrics.duplicate_stage_keys > 0:
        issues.append("重複階段鍵")
    if metrics.corner_coverage is None:
        issues.append("角球覆蓋缺失")
    elif metrics.corner_coverage < 1.0:
        issues.append("角球覆蓋不足")
    if metrics.stale_missing_corner_fixtures > 0:
        issues.append("逾期缺角球")
    if metrics.unresolved_fixtures > 0:
        issues.append("未解賽果")
    if metrics.stale_unresolved_fixtures > 0:
        issues.append("逾期未解賽果")
    return SystemHealth(system, metrics, tuple(issues))


def evaluate_reports(
    report_paths: Mapping[str, Path], *, generation_failed: bool = False
) -> HealthEvaluation:
    """Evaluate both systems in a stable order; never silently omit either."""
    return HealthEvaluation(
        tuple(evaluate_report(system, Path(report_paths[system])) for system in SYSTEMS),
        generation_failed=generation_failed,
    )


def _format_coverage(value: float | None) -> str:
    return "缺失" if value is None else f"{value:.1%}"


def format_message(evaluation: HealthEvaluation, now: datetime | None = None) -> str:
    """Build the concise, deterministic Traditional Chinese/Cantonese alert."""
    moment = (now or datetime.now(HKT)).astimezone(HKT)
    lines = [
        "足破／皇冠資料健康異常",
        f"{moment.strftime('%Y-%m-%d %H:%M:%S')} HKT",
    ]
    if evaluation.generation_failed:
        lines.append("資料健康重生失敗；報告可能唔係今個週期。")
    for system_health in evaluation.systems:
        label = SYSTEM_LABELS[system_health.system]
        if system_health.metrics is None:
            lines.append(
                f"{label}：重複=N/A；角球覆蓋=N/A；逾期缺角=N/A；"
                f"未解總數=N/A；逾期未解=N/A（{system_health.malformed_reason}）"
            )
            continue
        metrics = system_health.metrics
        details = (
            f"重複={metrics.duplicate_stage_keys}；"
            f"角球覆蓋={_format_coverage(metrics.corner_coverage)}；"
            f"逾期缺角={metrics.stale_missing_corner_fixtures}；"
            f"未解總數={metrics.unresolved_fixtures}；"
            f"逾期未解={metrics.stale_unresolved_fixtures}"
        )
        if system_health.issues:
            details += f"（{'、'.join(system_health.issues)}）"
        lines.append(f"{label}：{details}")
    lines.extend(("重試：", SETTLE_RETRY_URL, AUDIT_RETRY_URL))
    return "\n".join(lines)


def send_telegram(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    """Send with the direct Bot API only; there is deliberately no fallback."""
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID 未設定；無法發送資料健康異常通知")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 未設定；無法發送資料健康異常通知")
    body = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram 資料健康通知發送失敗:{type(exc).__name__}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        description = result.get("description", "unknown") if isinstance(result, dict) else "invalid_response"
        raise RuntimeError(f"Telegram 資料健康通知發送失敗:{description}")
    return "telegram_bot_api_ok"


def load_telegram_environment(
    env_file: Path | None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Read only Telegram assignments from an env file without executing it.

    The deployment environment contains unrelated secrets, so shell-sourcing
    it would permit arbitrary shell expansion.  This deliberately accepts only
    the two needed keys and treats every value as literal text.
    """
    values = dict(os.environ if environ is None else environ)
    if env_file is None:
        return values
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"無法安全讀取 Telegram 環境檔:{type(exc).__name__}") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            prefix = f"{key}="
            if not line.startswith(prefix):
                continue
            value = line[len(prefix):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
            break
    return values


def main(
    argv: Sequence[str] | None = None,
    *,
    sender: Callable[..., str] = send_telegram,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--footbreak-report",
        type=Path,
        default=Path("/var/www/footbreak/data-health.json"),
    )
    parser.add_argument(
        "--crown-report", type=Path, default=Path("/var/www/crown/data-health.json")
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Read only TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as literal assignments.",
    )
    parser.add_argument(
        "--generation-failed",
        action="store_true",
        help="Alert even if existing artifacts happen to look healthy.",
    )
    args = parser.parse_args(argv)
    evaluation = evaluate_reports(
        {"footbreak": args.footbreak_report, "crown": args.crown_report},
        generation_failed=args.generation_failed,
    )
    moment = (now or datetime.now(HKT)).astimezone(HKT)
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S HKT")
    if not evaluation.abnormal:
        print(f"{stamp} data-health alert: normal; Telegram not sent")
        return 0

    try:
        environment = load_telegram_environment(args.env_file, environ)
        sender(
            format_message(evaluation, now=moment),
            bot_token=environment.get("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=environment.get("TELEGRAM_CHAT_ID", "").strip(),
        )
    except Exception as exc:
        print(f"{stamp} data-health alert delivery failed: {exc}", file=sys.stderr)
        return 1
    print(f"{stamp} data-health alert: Telegram sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
