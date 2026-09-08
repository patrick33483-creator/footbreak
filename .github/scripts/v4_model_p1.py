"""V4 Phase 1 baseline: snapshot lock only, NO suggestions.

A no-vig probability alone cannot beat the market by construction — EV = -vig
for both sides. So Phase 1 does not emit any 提議; it only locks the three
時點 snapshots for later inspection. Phase 2 introduces the 5-sub-model
ensemble that produces true alpha vs the closing line.
"""
import json
from typing import Dict, Any, Optional
from v4_common import hk_to_decimal, implied_prob_from_hk, no_vig_two_way


THRESH_MIN_DEC = 1.7   # user's rule
THRESH_MIN_EV = 0.03   # minimum implied EV to include


def predict_two_way(snap: Optional[Dict[str, Any]], side_labels=("主", "客")) -> list:
    """Phase 1: intentionally returns [] — no-vig prob has zero alpha vs the
    market. Phase 2 will replace this with the ensemble output."""
    return []
    if not snap:
        return []
    home_hk = snap.get("home")
    away_hk = snap.get("away")
    dec_h = hk_to_decimal(home_hk)
    dec_a = hk_to_decimal(away_hk)
    if not dec_h or not dec_a:
        return []
    p_h_raw = implied_prob_from_hk(home_hk)
    p_a_raw = implied_prob_from_hk(away_hk)
    p_h, p_a = no_vig_two_way(p_h_raw, p_a_raw)
    out = []
    for side_idx, (side_lab, dec, p_model) in enumerate([
        (side_labels[0], dec_h, p_h),
        (side_labels[1], dec_a, p_a),
    ]):
        if dec < THRESH_MIN_DEC:
            continue
        if p_model is None:
            continue
        ev = round(p_model * dec - 1.0, 4)
        if ev < THRESH_MIN_EV:
            continue
        out.append({
            "side": side_lab,
            "line": snap.get("h"),
            "decimal_odds": dec,
            "hk_odds": home_hk if side_idx == 0 else away_hk,
            "model_prob": p_model,
            "ev": ev,
            "confidence": "低" if ev < 0.05 else ("中" if ev < 0.1 else "高"),
            "basis": "P1: 市場 no-vig 概率 vs 賠價",
        })
    return out


def predict_market_bundle(snapshots: Dict[str, Any], stage: str) -> Dict[str, Any]:
    """Given a match's snapshots dict + stage, return per-market suggestions.

    stage is one of INITIAL/T30/T5, mapped to the correct snap keys."""
    stage_map = {"INITIAL": ("initial_AH", "initial_OU"),
                 "T30": ("T30_AH", "T30_OU"),
                 "T5": ("T5_AH", "T5_OU")}
    if stage not in stage_map:
        return {"ah": [], "ou": [], "corners": []}
    ah_key, ou_key = stage_map[stage]
    ah_snap = snapshots.get(ah_key)
    ou_snap = snapshots.get(ou_key)
    return {
        "ah": predict_two_way(ah_snap, ("主", "客")),
        "ou": predict_two_way(ou_snap, ("大", "細")),
        "corners": [],  # phase 1: 皇冠冇提供角球盤, Phase 3 前接 opticodds
    }
