"""Runtime configuration loaded from environment variables / .env."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Anthropic
    kalshi_agent_model: str = "claude-opus-5"
    kalshi_agent_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    # Kalshi
    kalshi_env: Literal["demo", "prod"] = "demo"
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./kalshi_private_key.pem"

    # Safety switches
    dry_run: bool = True
    kill_switch: bool = False

    # Risk limits (cents / contracts)
    max_order_cost_cents: int = Field(default=2500, ge=0)
    max_position_contracts_per_market: int = Field(default=100, ge=0)
    max_total_exposure_cents: int = Field(default=20000, ge=0)
    max_orders_per_session: int = Field(default=20, ge=0)
    max_session_loss_cents: int = Field(default=5000, ge=0)
    max_slippage_cents: int = Field(default=3, ge=0)
    allowed_ticker_prefixes: Annotated[list[str], NoDecode] = []
    blocked_ticker_prefixes: Annotated[list[str], NoDecode] = []

    audit_log_path: str = "logs/audit.jsonl"

    @field_validator("allowed_ticker_prefixes", "blocked_ticker_prefixes", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip().upper() for p in v.split(",") if p.strip()]
        return v

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.kalshi_env]
