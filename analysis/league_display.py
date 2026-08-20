"""Exact-only Traditional-Chinese league names for user-facing messages."""

from __future__ import annotations


EXACT_LEAGUE_NAMES = {
    "USA - Major League Soccer": "美國職業足球大聯盟",
    "Chile - Primera Division": "智利甲組聯賽",
    "Brazil - Serie A": "巴西甲組聯賽",
    "Argentina - Liga Pro": "阿根廷甲組聯賽",
    "Argentina - Liga Profesional": "阿根廷甲組聯賽",
    "England - Premier League": "英格蘭超級聯賽",
    "England - Championship": "英格蘭冠軍聯賽",
    "Spain - LaLiga": "西班牙甲組聯賽",
    "Spain - Segunda Division": "西班牙乙組聯賽",
    "Italy - Serie A": "意大利甲組聯賽",
    "Italy - Serie B": "意大利乙組聯賽",
    "Germany - Bundesliga": "德國甲組聯賽",
    "Germany - 2. Bundesliga": "德國乙組聯賽",
    "France - Ligue 1": "法國甲組聯賽",
    "Netherlands - Eredivisie": "荷蘭甲組聯賽",
    "Portugal - Primeira Liga": "葡萄牙超級聯賽",
    "Japan - J1 League": "日本職業足球甲組聯賽",
    "South Korea - K League 1": "南韓職業足球甲組聯賽",
    "Australia - A-League": "澳洲職業足球聯賽",
    "UEFA Champions League": "歐洲聯賽冠軍盃",
    "UEFA Europa League": "歐洲足協歐霸盃",
}


def traditional_chinese_league(value: object) -> str:
    """Return an exact reviewed translation, otherwise preserve the source."""

    raw = str(value or "").strip()
    return EXACT_LEAGUE_NAMES.get(raw, raw)
