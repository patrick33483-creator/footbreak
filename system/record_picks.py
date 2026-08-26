"""三段 Footbreak 預測記錄與固定注碼條件模擬倉。

所有階段均保存為預測證據；只有首次保存的 T-5 快照，才可依據已結算
歷史細緻條件建立 HK$1,000 模擬注。T-30、重跑和歷史回填永不建注。
"""
import json
import hashlib
import os
import sys
import tempfile
import datetime as dt
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analysis.learning_store import LearningStore
from condition_portfolio import (
    AUDIT_LIMIT, DECISION_STAGE, LOG_LIMIT, STARTING_BANKROLL, evaluate_new_t5,
    evaluate_stage as evaluate_wilson_stage,
)
from probability_research import evaluate_new_t5 as evaluate_probability_research
from analysis.quarter_line import from_dixon_coles
from analysis.wilson_validation import ensure_namespace, recompute_namespace
from settle import condition_bets, recompute
from crown_execution_test import (
    DISPLAY_NAME as CROWN_EXECUTION_DISPLAY_NAME,
    capture_t5_counterparts,
    evaluate_new_t5 as evaluate_crown_execution_t5,
    prefetch_bridge as prefetch_crown_bridge,
    recompute as recompute_crown_execution,
)
try:
    import native_stage_state as stage_state
except ImportError:  # package import used by offline tests
    from system import native_stage_state as stage_state

# Production keeps this root-only ledger at the application path.  An
# explicit path is also injected for Crown's read-only reciprocal handoff;
# tests may still replace this module variable without changing deployment.
LEDGER = os.environ.get("FOOTBREAK_LEDGER_PATH", os.path.join(HERE, "sim_ledger.json"))
HKT = dt.timezone(dt.timedelta(hours=8))

BANKROLL = STARTING_BANKROLL
BET_STAGE = DECISION_STAGE
MIN_BET_LEAD_SECONDS = 5   # 少過五秒，視為沒有可靠的賽前建立時間
ACCURACY_HISTORY = os.path.join(HERE, "accuracy_history.json")
GRANULAR_RANKING = os.path.join(HERE, "granular_condition_ranking.json")
PROBABILITY_EVIDENCE = os.path.join(HERE, "footbreak_probability_evidence.json")
PREDICTION_ERA = "2026-08-10-market-learning-v2"
PREDICTION_SCHEMA_VERSION = 2


def load():
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        data = {"bankroll": BANKROLL, "bets": [], "log": [], "stats": {}, "watch": {}}
    data.setdefault("watch", {})
    data.setdefault("bets", [])
    data.setdefault("log", [])
    data.setdefault("stats", {})
    ensure_namespace(data, "footbreak")
    # Retired state stays untouched until the explicit reset, but active code
    # does not create, display, settle, or otherwise read it.
    data["log"] = [entry for entry in data["log"] if isinstance(entry, dict)][-LOG_LIMIT:]
    return data


def save(led):
    directory = os.path.dirname(LEDGER) or "."
    fd, temporary = tempfile.mkstemp(prefix=".sim-ledger-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(led, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, LEDGER)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _slim_adjs(adjs):
    out = []
    for a in (adjs or []):
        out.append({k: a.get(k) for k in
                    ("tag", "reason", "conf", "goals", "corners", "sup")
                    if a.get(k) is not None})
    return out


def _quarter_line_settlement_profile(candidate, model):
    if str(candidate.get("code") or "").upper() != "HIL":
        return None
    return from_dixon_coles(
        line=candidate.get("line", candidate.get("condition")),
        side=candidate.get("side"),
        lh=(model or {}).get("lh"),
        la=(model or {}).get("la"),
        rho=(model or {}).get("rho"),
    )


def _market_predictions(candidates, observed_at=None, quarter_line_model=None):
    """每個市場保留一個與賠率/EV無關的正式方向。

    主線優先；同一條主線兩邊之中，揀模型條件勝率較高的一邊。完整盤口
    仍由即時 predictions.json 保留，歷史只需要一個不重複計權的樣本。
    """
    grouped = {}
    for candidate in candidates or []:
        code = candidate.get("code")
        if code not in {"HDC", "HIL", "CHL"}:
            continue
        grouped.setdefault(code, []).append(candidate)
    output = []
    for code, rows in grouped.items():
        main = [row for row in rows if row.get("is_main")]
        pool = main or rows
        def decided_probability(row):
            probability = float(row.get("prob") or 0)
            push = float(row.get("push") or 0)
            return probability / max(1e-9, 1.0 - push)
        best = max(pool, key=lambda row: (decided_probability(row), float(row.get("prob") or 0)))
        odds = best.get("odds")
        try:
            odds_valid = float(odds) > 1.0
        except (TypeError, ValueError):
            odds_valid = False
        quote_observed_at = best.get("observed_at") or best.get("source_at") or observed_at
        quote_complete = odds_valid and quote_observed_at is not None
        settlement_profile = _quarter_line_settlement_profile(
            best, quarter_line_model,
        )
        output.append({
            "code": code,
            "market": best.get("market"),
            "condition": best.get("condition"),
            # Preserve the selected market's exact numeric line for isolated
            # notification-only T-5 signals.  This does not affect pricing,
            # staking, settlement, model learning, or condition-portfolio
            # decision.
            "line": best.get("line", best.get("condition")),
            "side": best.get("side"),
            "label": best.get("label"),
            "odds": odds if quote_complete else None,
            # A stage row with no price must be visibly incomplete.  This is
            # intentionally data only: it does not change a decision, EV,
            # Kelly, stake, notification, or settlement path.
            "odds_status": "available" if quote_complete else "missing",
            "odds_reason": (
                None if quote_complete
                else (
                    "selected_quote_timestamp_unavailable"
                    if odds_valid else "selected_quote_unavailable"
                )
            ),
            # HKJC does not expose a tick timestamp per line.  The run
            # captures `selected_odds_observed_at` immediately after reading
            # the board, so keep it explicitly labelled as a board-observation
            # time rather than misrepresenting it as a provider quote tick.
            "observed_at": quote_observed_at,
            "observed_board_at": observed_at,
            "provider": best.get("provider") or "HKJC",
            "probability": round(decided_probability(best), 6),
            "win_probability": best.get("prob"),
            "push_probability": best.get("push"),
            "is_main": bool(best.get("is_main")),
            "source": best.get("source") or "hkjc_public_board",
            **(
                {"quarter_line_settlement": settlement_profile}
                if settlement_profile is not None else {}
            ),
        })
    return sorted(output, key=lambda row: row["code"])


def _odds_journal(r, market_predictions):
    """Compact selected-quote evidence retained in each immutable stage payload.

    The prediction engine does not expose a trustworthy historical tick time
    for every HKJC line.  When that is unavailable the journal says so rather
    than pretending that the prediction generation time was a quote time.
    """
    journal = []
    for item in market_predictions:
        journal.append({
            "code": item.get("code"), "line": item.get("line", item.get("condition")),
            "side": item.get("side"), "odds": item.get("odds"),
            "odds_status": item.get("odds_status"),
            "reason": item.get("odds_reason"),
            "source": item.get("source"),
            "provider": item.get("provider"),
            "observed_at": item.get("observed_at"),
            "observed_board_at": item.get("observed_board_at"),
        })
    return journal


def _snap(r, now):
    """把一次預測壓成一個階段記錄。"""
    can_bet = bool(r.get("can_bet"))
    pick = r.get("pick") or (r.get("lead_view") if not can_bet else None)
    cands = r.get("candidates") or []
    lead = cands[0] if cands else None
    wx = r.get("weather") or {}
    if can_bet:
        verdict = "落注" if r.get("pick") else "觀望"
    elif pick:
        verdict = "傾向"          # 已過信念門檻,但未到落注時點
    elif lead and lead.get("ev", -1) > 0:
        verdict = "偏向"          # 有正值方向,但信念未夠
    else:
        verdict = "無傾向"
    market_predictions = _market_predictions(
        cands, r.get("selected_odds_observed_at"), r.get("quarter_line_model"),
    )
    odds_journal = _odds_journal(r, market_predictions)
    missing_odds = [item for item in odds_journal if item["odds_status"] != "available"]
    return {
        "prediction_era": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "stage": r["stage"],
        "ts": now,
        "kickoff": r.get("kickoff_hkt"),
        # Fixture context is stored with every immutable stage snapshot so the
        # offline challenger can encode league/home-away context without
        # joining mutable live-watch state.
        "league": r.get("league"),
        "home": r.get("home"),
        "away": r.get("away"),
        "neutral": r.get("neutral"),
        "can_bet": can_bet,
        "mins_to_ko": r.get("mins_to_ko"),
        "conviction": r.get("conviction"),
        "model_source": r.get("model_source"),
        "sharp_reference_available": bool(r.get("sharp_reference_available")),
        "sharp_reference_note": r.get("sharp_reference_note"),
        # Source provenance is deliberately persisted with the immutable stage
        # snapshot so source-health analysis can be calculated entirely from
        # raw rows.  A fallback source never authorises a condition simulation
        # bet or notification.
        "provider_live": bool(r.get("provider_live")),
        "source": r.get("source"),
        "data_age_seconds": r.get("data_age_seconds"),
        "source_status": r.get("source_status"),
        "pinnapi_fixture_identity": r.get("pinnapi_fixture_identity"),
        "verdict": verdict,
        "no_bet_reason": r.get("no_bet_reason"),
        # 模型 pick 只屬預測記錄；建立模擬注另由歷史條件規則處理。
        "pick": ({"market": pick["market"], "code": pick["code"],
                  "condition": pick["condition"], "side": pick["side"],
                  "label": f"{pick['market']} {pick['label']}",
                  "odds": pick["odds"], "prob": pick["prob"],
                  "push": pick["push"], "ev": pick["ev"],
                  "kelly": pick["kelly_used"], "stake": pick["stake"]}
                 if pick else None),
        # 就算未夠信念,都記低模型最看好嘅一條
        "lead": ({"market": lead.get("market"), "label": lead.get("label"),
                  "odds": lead.get("odds"), "prob": lead.get("prob"),
                  "ev": lead.get("ev")} if lead else None),
        # 三段推演
        "open": r.get("open"),
        "now": r.get("now"),
        "final": r.get("final"),
        "quarter_line_model": r.get("quarter_line_model"),
        "movement": r.get("movement"),
        "adjustments": _slim_adjs(r.get("adjustments")),
        "mults": r.get("mults"),
        "outcome": r.get("outcome"),
        "market_predictions": market_predictions,
        # These top-level fields make no-price stage snapshots visible even
        # when no market direction was selectable at all.
        "odds_status": (
            "available" if odds_journal and not missing_odds
            else "missing"
        ),
        "odds_reason": (
            None if odds_journal and not missing_odds
            else ("no_selected_market_quote" if not odds_journal
                  else "one_or_more_selected_quotes_unavailable")
        ),
        "selected_odds_journal": odds_journal,
        # 資訊齊唔齊
        "info": {
            "weather": bool(wx),
            "temp": wx.get("temp_c"),
            "desc": wx.get("desc"),
            "news": bool(r.get("has_news")),
            "hk_lines": r.get("n_hk_lines"),
            "hk_moved": r.get("hk_moved_since_last"),
            "hk_max_move_pct": r.get("hk_max_move_pct"),
        },
    }


STAGE_ORDER = {"首預": 1, "T-30": 2, "T-5": 3}


def _record_learning_snapshot(mid, r, snap):
    path = os.environ.get("LEARNING_DB_PATH")
    if not path:
        return None
    kickoff = dt.datetime.fromisoformat(str(r["kickoff_hkt"]))
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=HKT)
    payload = {key: value for key, value in snap.items() if key != "ts"}
    with LearningStore(path) as store:
        return store.record_snapshot(
            "footbreak",
            mid,
            str(r["stage"]),
            snap["ts"],
            kickoff.isoformat(),
            payload,
            model_version=PREDICTION_ERA,
            schema_version=str(PREDICTION_SCHEMA_VERSION),
        )


def _condition_change(bet):
    rate = float(bet.get("condition_accuracy") or 0) * 100
    hits = int(bet.get("condition_hits") or 0)
    decided = int(bet.get("condition_decided") or 0)
    line = bet.get("selected_line")
    line_text = f" {float(line):g}" if isinstance(line, (int, float)) else ""
    return (
        f"{bet.get('home') or '—'} 對 {bet.get('away') or '—'} — 獨立驗證注："
        f"{bet.get('market_label') or '—'} {bet.get('selected_role') or '—'}{line_text}，"
        f"賠率 {float(bet.get('odds') or 0):.2f}，歷史命中率 {rate:.1f}%（{hits}/{decided}）"
    )


def _crown_execution_change(bet):
    """Public local log line for the separately funded cross-book simulation."""
    line = bet.get("selected_line")
    line_text = f" {float(line):g}" if isinstance(line, (int, float)) else ""
    return (
        f"{bet.get('home') or '—'} 對 {bet.get('away') or '—'} — "
        f"{CROWN_EXECUTION_DISPLAY_NAME}：{bet.get('market_label') or '—'} "
        f"{bet.get('selected_role') or '—'}{line_text}，"
        f"馬會訊號 {float(bet.get('hkjc_signal_odds') or 0):.2f}／"
        f"皇冠模擬 {float(bet.get('crown_execution_odds') or 0):.2f}"
    )


def _attach_persisted_crown_counterpart(ledger, watch, rows):
    """Copy same-decision optional counterpart evidence into alert rows only."""
    evidence = (((watch.get("counterpart_bridges") or {}).get("crown") or {})
                .get("t5") or {}).get("markets") or {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        market = str(row.get("code") or row.get("market") or "").upper()
        value = evidence.get(market)
        if isinstance(value, dict):
            row["crown_counterpart"] = {
                key: copy for key, copy in value.items()
                if key in {"status", "reason", "market", "side", "line",
                           "hkjc_observed_at", "crown_quote", "captured_at"}
            }


OPTIONAL_JOB_LOG = "native_post_commit_jobs"
OPTIONAL_JOB_TERMINAL = frozenset({"COMPLETED", "EXPIRED"})


def _cross_book_grace_deadline(snapshot, now):
    try:
        seconds = float(os.getenv("FOOTBREAK_CROWN_GRACE_SECONDS", "20"))
    except ValueError:
        seconds = 20.0
    seconds = max(0.0, min(30.0, seconds))
    stage_at = stage_state.parse_time(snapshot.get("ts")) or stage_state.parse_time(now)
    kickoff = stage_state.parse_time(snapshot.get("kickoff"))
    if stage_at is None or kickoff is None:
        return None
    return min(stage_at + dt.timedelta(seconds=seconds), kickoff).isoformat()


def _optional_job_events(ledger):
    events = ledger.get(OPTIONAL_JOB_LOG)
    if not isinstance(events, list):
        events = []
        ledger[OPTIONAL_JOB_LOG] = events
    return events


def _latest_optional_jobs(ledger):
    latest = {}
    for event in ledger.get(OPTIONAL_JOB_LOG) or []:
        if isinstance(event, dict) and event.get("job_id"):
            latest[str(event["job_id"])] = event
    return latest


def _job_snapshot_id(match_id, snapshot, result):
    attempt_id = str(result.get("_native_stage_attempt_id") or "")
    return (
        f"attempt:{attempt_id}" if attempt_id
        else f"snapshot:{match_id}:{snapshot.get('stage')}:{snapshot.get('ts')}"
    )


def _enqueue_optional_job(ledger, match_id, snapshot, result, *, now, t5_safe_to_evaluate):
    """Journal deferred consumers with the immutable native snapshot."""
    snapshot_id = _job_snapshot_id(match_id, snapshot, result)
    snapshot_payload = {
        key: value for key, value in snapshot.items()
        if key not in {"native_snapshot_id", "native_snapshot_hash"}
    }
    snapshot_hash = hashlib.sha256(json.dumps(
        snapshot_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    snapshot["native_snapshot_id"] = snapshot_id
    snapshot["native_snapshot_hash"] = snapshot_hash
    job_id = f"{match_id}:{snapshot_id}"
    previous = _latest_optional_jobs(ledger).get(job_id)
    if isinstance(previous, dict):
        return previous
    event = {
        "schema_version": 1, "job_id": job_id, "status": "PENDING", "at": now,
        "match_id": match_id, "stage": snapshot.get("stage"),
        "snapshot_id": snapshot_id,
        "t5_safe_to_evaluate": bool(t5_safe_to_evaluate),
    }
    if snapshot.get("stage") == BET_STAGE:
        event["cross_book_deadline_at"] = _cross_book_grace_deadline(snapshot, now)
    _optional_job_events(ledger).append(event)
    return event


def _optional_job_transition(ledger, job, status, *, now, reason=None):
    event = {
        key: job.get(key)
        for key in ("schema_version", "job_id", "match_id", "stage", "snapshot_id",
                    "t5_safe_to_evaluate", "cross_book_deadline_at")
    }
    event.update({"status": status, "at": now})
    if reason:
        event["reason"] = reason
    _optional_job_events(ledger).append(event)
    return event


def _job_watch_snapshot(ledger, job):
    watch = (ledger.get("watch") or {}).get(str(job.get("match_id") or ""))
    if not isinstance(watch, dict):
        return None, None
    snapshot = next((
        row for row in watch.get("stages") or []
        if isinstance(row, dict)
        and str(row.get("native_snapshot_id") or "") == str(job.get("snapshot_id") or "")
        and str(row.get("stage") or "") == str(job.get("stage") or "")
    ), None)
    return watch, snapshot


def _native_snapshot_hash_valid(snapshot):
    expected = snapshot.get("native_snapshot_hash")
    if expected is None:
        # Jobs created before native snapshot hashing retain their v1 semantics.
        return True
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    payload = {
        key: value for key, value in snapshot.items()
        if key not in {"native_snapshot_id", "native_snapshot_hash"}
    }
    actual = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    return actual == expected


def _load_frozen_ranking():
    try:
        payload = json.loads(Path(GRANULAR_RANKING).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    return (
        payload.get("ranking")
        if isinstance(payload, dict) and payload.get("system") == "footbreak"
        and payload.get("schema_version") == 1
        else None
    )


def _process_optional_job(ledger, job, *, now, changes):
    """Run sidecars/evaluation only from an already durable native snapshot."""
    watch, snapshot = _job_watch_snapshot(ledger, job)
    if not isinstance(watch, dict) or not isinstance(snapshot, dict):
        raise ValueError("native_snapshot_missing_for_optional_job")
    if not _native_snapshot_hash_valid(snapshot):
        raise ValueError("native_snapshot_hash_mismatch")
    stage, match_id = str(job.get("stage") or ""), str(job.get("match_id") or "")
    # Learning is deliberately a side effect of a committed immutable snapshot,
    # never a prerequisite for the native stage transaction.
    _record_learning_snapshot(match_id, {
        "kickoff_hkt": snapshot.get("kickoff"),
        "stage": stage,
    }, snapshot)
    if stage in {"首預", "T-30"}:
        field = "first_look" if stage == "首預" else "t30"
        try:
            prefetch_crown_bridge(watch, stage=stage, now=now, ledger=ledger)
        except Exception as exc:
            watch.setdefault("counterpart_bridges", {}).setdefault("crown", {})[field] = {
                "at": now, "status": "UNAVAILABLE",
                "reason": f"crown_sidecar_local_error:{type(exc).__name__}",
            }
        ranking = _load_frozen_ranking()
        # The optional job exists in the same atomic write as the native row,
        # and is drained only after that write has completed.
        evaluate_wilson_stage(
            ledger, watch, stage, history_path=Path(ACCURACY_HISTORY),
            ranking=ranking if isinstance(ranking, list) else None,
        )
        return
    if stage != BET_STAGE:
        return
    t5_safe_to_evaluate = bool(job.get("t5_safe_to_evaluate"))
    if not t5_safe_to_evaluate:
        ledger["wilson_validation"]["audit"].append({
            "ts": now, "match_id": match_id, "market": "*",
            "status": "SKIPPED", "reason": "t5_safe_lead_not_met",
        })
    ranking = _load_frozen_ranking()
    created, observations = [], []
    if t5_safe_to_evaluate:
        created, _audit = evaluate_new_t5(
            ledger, watch, history_path=Path(ACCURACY_HISTORY),
            ranking=ranking if isinstance(ranking, list) else None,
        )
        observations = [
            row for row in ((ledger.get("wilson_validation") or {}).get("observations") or [])
            if isinstance(row, dict) and str(row.get("match_id") or "") == match_id
            and str(row.get("stage") or "") == BET_STAGE
            and str(row.get("created_at") or "") == str(snapshot.get("ts") or "")
        ]
        evaluate_probability_research(
            ledger, watch, ranking=ranking if isinstance(ranking, list) else None,
            evidence_path=Path(PROBABILITY_EVIDENCE),
        )
        if created:
            ledger["bets"].extend(created)
            changes.extend(_condition_change(bet) for bet in created)
    try:
        captured = capture_t5_counterparts(
            watch, now=now, ledger=ledger,
            grace_deadline_at=job.get("cross_book_deadline_at"),
        )
    except Exception as exc:
        captured = {}
        watch.setdefault("counterpart_bridges", {}).setdefault("crown", {})["t5"] = {
            "at": now, "markets": {}, "reason": f"crown_sidecar_local_error:{type(exc).__name__}",
        }
    _attach_persisted_crown_counterpart(ledger, watch, list(created) + observations)
    if any(
        isinstance(row, dict) and row.get("status") == "PENDING"
        for row in captured.values()
    ):
        return "DEFERRED"
    crown_created = []
    if t5_safe_to_evaluate:
        try:
            crown_created, _crown_audit = evaluate_crown_execution_t5(
                ledger, watch, ranking=ranking if isinstance(ranking, list) else None, now=now,
                decision_at=job.get("cross_book_deadline_at"),
            )
        except Exception as exc:
            (ledger.get("wilson_validation") or {}).setdefault("audit", []).append({
                "ts": now, "match_id": match_id, "market": "*", "status": "SKIPPED",
                "reason": f"crown_execution_sidecar_local_error:{type(exc).__name__}",
            })
    if crown_created:
        changes.extend(_crown_execution_change(bet) for bet in crown_created)
    return "COMPLETED"


def _drain_optional_jobs(ledger, *, now, changes, notes):
    """Retry durable post-commit work without rolling back native evidence."""
    for _job_id, job in list(_latest_optional_jobs(ledger).items()):
        if str(job.get("status") or "") in OPTIONAL_JOB_TERMINAL:
            continue
        watch, snapshot = _job_watch_snapshot(ledger, job)
        if (
            str(job.get("stage") or "") == BET_STAGE
            and not job.get("cross_book_deadline_at")
            and isinstance(snapshot, dict)
        ):
            job = {
                **job,
                "cross_book_deadline_at": _cross_book_grace_deadline(
                    snapshot, snapshot.get("ts") or now,
                ),
            }
        kickoff = stage_state.parse_time(
            snapshot.get("kickoff") if isinstance(snapshot, dict) else None
        )
        current = stage_state.parse_time(now)
        if kickoff is None or current is None or current >= kickoff:
            if str(job.get("stage") or "") == BET_STAGE and job.get("t5_safe_to_evaluate"):
                ledger["wilson_validation"]["audit"].append({
                    "ts": now, "match_id": job.get("match_id"), "market": "*",
                    "status": "SKIPPED", "reason": "optional_job_expired_before_native_evaluation",
                })
            _optional_job_transition(
                ledger, job, "EXPIRED", now=now,
                reason="kickoff_elapsed_before_optional_consumer",
            )
            continue
        _optional_job_transition(ledger, job, "STARTED", now=now)
        try:
            outcome = _process_optional_job(ledger, job, now=now, changes=changes)
        except Exception as exc:
            _optional_job_transition(
                ledger, job, "RETRYABLE_FAILURE", now=now,
                reason=f"optional_consumer_error:{type(exc).__name__}",
            )
            notes.append(
                f"{job.get('match_id') or '—'} — {job.get('stage') or '—'} "
                f"optional consumer failed ({type(exc).__name__}); native snapshot remains committed"
            )
            continue
        if outcome == "DEFERRED":
            _optional_job_transition(
                ledger, job, "WAITING", now=now,
                reason="crown_counterpart_grace_pending",
            )
            continue
        _optional_job_transition(ledger, job, "COMPLETED", now=now)


def drain_post_commit_jobs():
    """Retry optional consumers independently of a new native stage arrival."""
    ledger = load()
    now = dt.datetime.now(HKT).isoformat(timespec="seconds")
    changes, notes = [], []
    _drain_optional_jobs(ledger, now=now, changes=changes, notes=notes)
    recompute(ledger)
    recompute_crown_execution(ledger)
    save(ledger)
    return changes, notes, ledger


def sync(preds_file="predictions.json", *, send_notifications=True):
    """Persist prediction snapshots and evaluate only newly saved T-5 rows.

    Native stage evidence is committed before any optional consumer. A duplicate
    stage leaves the immutable snapshot alone, while its durable consumer job
    may still be retried idempotently.
    """
    ledger = load()
    with open(os.path.join(HERE, preds_file), encoding="utf-8") as handle:
        predictions = json.load(handle)
    now = dt.datetime.now(HKT).isoformat(timespec="seconds")
    recorded_at = dt.datetime.fromisoformat(now)
    changes, notes = [], []

    for result in predictions:
        match_id = str(result["match_id"])
        stage = result["stage"]
        if stage not in STAGE_ORDER:
            continue
        result_kickoff = stage_state.parse_time(result.get("kickoff_hkt"))
        if result_kickoff is None or result_kickoff <= recorded_at:
            attempt_id = str(result.get("_native_stage_attempt_id") or "")
            if stage in {"T-30", "T-5"} and attempt_id:
                attempt = next((
                    row for row in reversed(ledger.get("native_stage_attempts") or [])
                    if isinstance(row, dict)
                    and row.get("attempt_id") == attempt_id
                    and row.get("status") == "STARTED"
                ), None)
                if isinstance(attempt, dict):
                    stage_state.finish_attempt(
                        ledger, attempt, "EXPIRED", now=recorded_at,
                        reason="kickoff_elapsed_before_native_snapshot",
                    )
            notes.append(
                f"{result.get('home') or '—'} v {result.get('away') or '—'} — "
                f"{stage} 賽後結果已隔離，沒有建立 native stage"
            )
            continue
        t5_safe_to_evaluate = True
        if stage == BET_STAGE:
            try:
                kickoff = dt.datetime.fromisoformat(str(result["kickoff_hkt"]))
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=HKT)
                lead = (kickoff - dt.datetime.now(HKT)).total_seconds()
            except (KeyError, TypeError, ValueError):
                lead = -1
            if lead < MIN_BET_LEAD_SECONDS:
                t5_safe_to_evaluate = False
                notes.append(
                    f"{result.get('home') or '—'} v {result.get('away') or '—'} — "
                    "T-5 已保存為預測證據，但到帳本前已過安全落注時間，沒有建立條件模擬注"
                )

        watch = ledger["watch"].setdefault(match_id, {
            "match_id": match_id, "league": result.get("league"),
            "home": result.get("home"), "away": result.get("away"),
            "home_en": result.get("home_en"), "away_en": result.get("away_en"),
            "kickoff": result.get("kickoff_hkt"), "fixture_id": result.get("fixture_id"),
            "league_id": result.get("league_id"), "venue": result.get("venue"),
            "venue_city": result.get("venue_city"), "stages": [],
        })
        for key in ("league", "home", "away", "home_en", "away_en", "kickoff", "fixture_id", "league_id", "venue", "venue_city"):
            value = result.get("kickoff_hkt") if key == "kickoff" else result.get(key)
            if value is not None:
                watch[key] = value
        previous = next((row for row in watch["stages"] if row.get("stage") == stage), None)
        if previous is not None:
            notes.append(
                f"{result.get('home') or '—'} v {result.get('away') or '—'} — "
                f"{stage} 已保存；保留原始記錄"
            )
            continue

        snapshot = _snap(result, now)
        # A genuine first-look snapshot atomically creates native scheduler
        # metadata only.  It never fabricates T-30/T-5 evidence.  Timed rows
        # inherit that immutable HKJC identity/due schedule redundantly.
        if stage == "首預":
            stage_state.ensure_first_look_manifest(
                watch, now=dt.datetime.fromisoformat(now),
            )
        elif stage in {"T-30", "T-5"} and not isinstance(
            watch.get("native_stage_manifest"), dict,
        ):
            kickoff = stage_state.parse_time(watch.get("kickoff"))
            if kickoff is not None and kickoff > dt.datetime.now(HKT):
                stage_state.ensure_manifest(
                    watch, origin="migration_existing_future_card",
                    now=dt.datetime.fromisoformat(now),
                )
        stage_state.enrich_snapshot(snapshot, watch, stage)
        if stage == BET_STAGE:
            snapshot["t30_data_complete"] = any(
                item.get("stage") == "T-30" for item in watch["stages"]
            )
        watch["stages"].append(snapshot)
        watch["stages"].sort(key=lambda row: STAGE_ORDER.get(row.get("stage"), 9))
        attempt_id = str(result.get("_native_stage_attempt_id") or "")
        if stage in {"T-30", "T-5"} and attempt_id:
            attempt = next((
                row for row in reversed(ledger.get("native_stage_attempts") or [])
                if isinstance(row, dict)
                and row.get("attempt_id") == attempt_id
                and row.get("status") == "STARTED"
            ), None)
            if isinstance(attempt, dict):
                # This transition and the immutable native snapshot share the
                # same later `save(ledger)` replacement.
                stage_state.finish_attempt(
                    ledger, attempt, "COMMITTED",
                    now=dt.datetime.fromisoformat(now),
                )
        _enqueue_optional_job(
            ledger, match_id, snapshot, result, now=now,
            t5_safe_to_evaluate=t5_safe_to_evaluate,
        )
        label = (snapshot.get("pick") or {}).get("label") or "無明顯傾向"
        notes.append(f"{result.get('home') or '—'} v {result.get('away') or '—'} — {stage} {snapshot['verdict']}：{label}")

    # Native stage, expected-job record, and terminal attempt are durable
    # before any sidecar, matcher, research, or notification work is entered.
    save(ledger)
    _drain_optional_jobs(ledger, now=now, changes=changes, notes=notes)
    recompute(ledger)
    recompute_crown_execution(ledger)
    ledger["log"].append({
        "ts": now, "kind": "預測", "n_changes": len(changes),
        "changes": (changes or ["本次沒有建立條件模擬注"])[:LOG_LIMIT],
        "notes": notes[:40],
    })
    ledger["log"] = ledger["log"][-LOG_LIMIT:]
    save(ledger)

    # v1 granular candidate entry notices are retired at the Wilson cutover.
    # Only committed Wilson bets are eligible for the durable outbox below.
    if send_notifications:
        try:
            import notify
            # One post-commit delivery envelope covers both isolated
            # portfolios.  A failed/slow Wilson retry cannot double the
            # Footbreak tick's notification attempt budget via cross-book.
            # This supersedes the former `notify_pending_condition_bets(ledger)`
            # followed by a separate cross-book outbox call; both remain
            # durable and retryable inside notify_pending_committed_bets().
            notify.notify_pending_committed_bets(ledger, max_attempts=1, max_seconds=5)
        except Exception as exc:
            notes.append(
                f"條件模擬注通知發送失敗（{type(exc).__name__}）；"
                "已保存模擬注，下一輪會自動重試。"
            )
    return changes, notes, ledger


def summary(led):
    bets = condition_bets(led)
    pend = [b for b in bets if b["status"] == "PENDING"]
    void = [b for b in bets if b["status"] == "VOIDED"]
    done = [b for b in bets if b["status"] == "SETTLED"]
    tot = sum(b["stake"] for b in pend)
    pnl = sum(b.get("pnl") or 0 for b in done)
    n_st = sum(len(w.get("stages") or []) for w in led["watch"].values())
    return {"追蹤賽事": len(led["watch"]), "階段預測": n_st,
            "待決": len(pend), "已撤": len(void), "已結算": len(done),
            "在場注碼": tot, "在場佔本金": f"{tot/BANKROLL:.1%}",
            "累計盈虧": pnl}


def export_csv(led, path=None):
    import csv
    path = path or os.path.join(HERE, "sim_ledger.csv")
    cols = ["kickoff", "league", "home", "away", "market", "label", "odds",
            "stake", "model_prob", "push_prob", "ev", "conviction",
            "first_stage", "stage", "status", "result", "pnl", "n_updates"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["開賽時間", "聯賽", "主隊", "客隊", "市場", "投注",
                    "賠率", "注碼", "模型勝率", "走水率", "EV", "信念",
                    "落注階段", "最新階段", "狀態", "賽果", "盈虧", "更新次數"])
        for b in condition_bets(led):
            w.writerow([b.get(c) if c != "n_updates" else len(b.get("history") or [])
                        for c in cols])
    return path


def export_watch_csv(led, path=None):
    """三段預測記錄 CSV — 每場每階段一行,方便喺 Excel 對比演變。"""
    import csv
    path = path or os.path.join(HERE, "sim_watch.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["開賽時間", "聯賽", "主隊", "客隊", "階段", "預測時間",
                    "結論", "建議/最看好", "賠率", "模型勝率", "EV", "信念",
                    "初盤總入球", "現價總入球", "我終值總入球",
                    "初盤主客差", "現價主客差", "我終值主客差",
                    "角球 μ", "氣溫", "有陣容資訊", "馬會線數", "觀望原因"])
        for m in sorted(led["watch"].values(), key=lambda x: x["kickoff"]):
            for s in m.get("stages", []):
                p = s.get("pick") or s.get("lead") or {}
                op, nw, fi = s.get("open") or {}, s.get("now") or {}, s.get("final") or {}
                info = s.get("info") or {}
                w.writerow([m["kickoff"], m["league"], m["home"], m["away"],
                            s["stage"], s["ts"], s["verdict"],
                            p.get("label"), p.get("odds"), p.get("prob"),
                            p.get("ev"), s.get("conviction"),
                            op.get("total"), nw.get("total"), fi.get("total"),
                            op.get("supremacy"), nw.get("supremacy"), fi.get("supremacy"),
                            fi.get("mu"), info.get("temp"),
                            "有" if info.get("news") else "冇",
                            info.get("hk_lines"), s.get("no_bet_reason")])
    return path


def notify_only(led):
    """Retry all durable Footbreak notification outboxes with native priority."""
    import notify
    return notify.notify_pending_committed_bets(led)


if __name__ == "__main__":
    import sys
    if "--notify-only" in sys.argv[1:]:
        led = load()
        try:
            notify_only(led)
        except Exception as exc:
            print(f"通知暫不可用（{type(exc).__name__}）；下一輪重試")
        raise SystemExit(0)
    ch, notes, led = sync(send_notifications="--no-notify" not in sys.argv[1:])
    print("\n".join(notes) if notes else "無新階段預測")
    print()
    print("\n".join(ch) if ch else "無落注動作")
    print()
    for k, v in summary(led).items():
        print(f"  {k}: {v}")
    print("\n注單 CSV →", export_csv(led))
    print("預測 CSV →", export_watch_csv(led))
