"""Crown configuration.  Secrets are read only from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Only simulation is implemented; there is deliberately no real-bet switch."""

    state_dir: Path
    app_dir: Path
    web_root: Path
    enabled: bool
    pinnapi_key: str | None
    pinnapi_base_url: str
    source_max_age_seconds: int
    allow_inferred_pinnapi_timestamp: bool
    titan_bf_base: str
    titan_vip_base: str
    titan_company_id: str
    telegram_enabled: bool
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    confidence_floor: float
    min_edge: float
    bankroll: float

    @property
    def pinnapi_configured(self) -> bool:
        return bool(self.pinnapi_key)


def settings() -> Settings:
    root = Path(__file__).resolve().parent
    key = (os.getenv("CUSTOM_CRED_PINNAPI_COM_TOKEN") or os.getenv("PINNAPI_API_KEY") or "").strip() or None
    base = (os.getenv("PINNAPI_BASE_URL") or os.getenv("CUSTOM_CRED_PINNAPI_COM_URL") or "https://pinnapi.com").strip()
    # Endpoint roots must not contain credentials or query strings.
    if not (base.startswith("https://") or base.startswith("http://")):
        base = "https://pinnapi.com"
    base = base.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return Settings(
        state_dir=Path(os.getenv("CROWN_STATE_DIR", "/var/lib/footbreak/crown")),
        app_dir=Path(os.getenv("CROWN_APP_DIR", str(root))),
        web_root=Path(os.getenv("CROWN_WEB_ROOT", "/var/www/crown")),
        enabled=_flag("CROWN_ENABLED", False),
        pinnapi_key=key,
        pinnapi_base_url=base,
        source_max_age_seconds=max(1, int(_number("CROWN_SOURCE_MAX_AGE_SEC", 90))),
        allow_inferred_pinnapi_timestamp=_flag("CROWN_ALLOW_INFERRED_PINNAPI_TIMESTAMP", False),
        titan_bf_base=os.getenv("CROWN_TITAN_BF_BASE", os.getenv("TITAN_BF_BASE", "http://bf.titan007.com/football")).rstrip("/"),
        titan_vip_base=os.getenv("CROWN_TITAN_VIP_BASE", os.getenv("TITAN_VIP_BASE", "http://vip.titan007.com")).rstrip("/"),
        titan_company_id=os.getenv("CROWN_TITAN_COMPANY_ID", "3").strip(),
        telegram_enabled=_flag("CROWN_TELEGRAM_ENABLED", False),
        telegram_bot_token=(
            os.getenv("CROWN_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or ""
        ).strip() or None,
        telegram_chat_id=(
            os.getenv("CROWN_TELEGRAM_CHAT_ID")
            or os.getenv("TELEGRAM_CHAT_ID")
            or ""
        ).strip() or None,
        confidence_floor=_number("CROWN_CONF_FLOOR", 58.0),
        min_edge=_number("CROWN_MIN_EDGE", 0.02),
        bankroll=_number("CROWN_BANKROLL", 50000.0),
    )
