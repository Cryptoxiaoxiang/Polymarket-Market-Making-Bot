from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    private_key: str | None = None
    funder: str | None = None
    signature_type: int = 0
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    order_journal_path: str = ".poly-mm-orders.json"
    data_api_url: str = "https://data-api.polymarket.com"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    user_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    @classmethod
    def from_env(cls, path: str | Path = ".env") -> "Settings":
        _load_dotenv(Path(path))
        return cls(
            private_key=os.getenv("POLYMARKET_PRIVATE_KEY") or None,
            funder=os.getenv("POLYMARKET_FUNDER_ADDRESS") or None,
            signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
            api_key=os.getenv("POLYMARKET_API_KEY") or None,
            api_secret=os.getenv("POLYMARKET_API_SECRET") or None,
            api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE") or None,
            order_journal_path=os.getenv(
                "POLYMARKET_ORDER_JOURNAL_PATH", ".poly-mm-orders.json"
            ),
        )


@dataclass(frozen=True)
class MarketConfig:
    token_id: str = ""
    url: str = ""
    outcome: str = "Yes"
    market_slug: str = ""
    condition_id: str = ""
    label: str = ""
    enabled: bool = True
    quote_size: Decimal | None = None


@dataclass(frozen=True)
class StrategyConfig:
    quote_size: Decimal = Decimal("5")
    join_best_price: bool = False
    min_edge_ticks: int = 1
    min_spread: Decimal = Decimal("0.02")
    max_spread: Decimal = Decimal("0.15")


@dataclass(frozen=True)
class RiskConfig:
    max_order_size: Decimal = Decimal("10")
    max_position_per_token: Decimal = Decimal("25")
    max_total_open_notional: Decimal = Decimal("100")
    max_open_orders_per_token: int = 1


@dataclass(frozen=True)
class BotConfig:
    dry_run: bool = False
    poll_interval_seconds: float = 2
    cancel_after_seconds: float = 10
    cancel_all_on_start: bool = True
    cancel_all_on_shutdown: bool = True
    halt_on_fill: bool = True
    preflight_enabled: bool = True
    websocket_enabled: bool = True
    position_poll_interval_seconds: float = 5
    cancel_retry_count: int = 4
    cancel_retry_base_seconds: float = 0.5
    console_enabled: bool = True
    console_host: str = "127.0.0.1"
    console_port: int = 8081
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    markets: list[MarketConfig] = field(default_factory=list)

    @property
    def enabled_markets(self) -> list[MarketConfig]:
        return [market for market in self.markets if market.enabled]


def load_config(path: str | Path, *, require_markets: bool = True) -> BotConfig:
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    strategy_raw, risk_raw = data.get("strategy", {}), data.get("risk", {})
    strategy = StrategyConfig(
        **_decimal_fields(strategy_raw, {"quote_size", "min_spread", "max_spread"})
    )
    risk = RiskConfig(
        **_decimal_fields(
            risk_raw,
            {"max_order_size", "max_position_per_token", "max_total_open_notional"},
        )
    )
    markets = [
        MarketConfig(**_decimal_fields(row, {"quote_size"})) for row in data.get("markets", [])
    ]
    config = BotConfig(
        dry_run=bool(data.get("dry_run", False)),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 2)),
        cancel_after_seconds=float(data.get("cancel_after_seconds", 10)),
        cancel_all_on_start=bool(data.get("cancel_all_on_start", True)),
        cancel_all_on_shutdown=bool(data.get("cancel_all_on_shutdown", True)),
        halt_on_fill=bool(data.get("halt_on_fill", True)),
        preflight_enabled=bool(data.get("preflight_enabled", True)),
        websocket_enabled=bool(data.get("websocket_enabled", True)),
        position_poll_interval_seconds=float(
            data.get("position_poll_interval_seconds", 5)
        ),
        cancel_retry_count=int(data.get("cancel_retry_count", 4)),
        cancel_retry_base_seconds=float(data.get("cancel_retry_base_seconds", 0.5)),
        console_enabled=bool(data.get("console_enabled", True)),
        console_host=str(data.get("console_host", "127.0.0.1")),
        console_port=int(data.get("console_port", 8081)),
        strategy=strategy,
        risk=risk,
        markets=markets,
    )
    if require_markets and not config.enabled_markets:
        raise ValueError("At least one enabled market is required")
    for market in config.enabled_markets:
        if not market.token_id and not market.url:
            raise ValueError("Each enabled market requires token_id or url")
    if config.poll_interval_seconds <= 0 or config.position_poll_interval_seconds <= 0:
        raise ValueError("Polling intervals must be positive")
    if config.cancel_after_seconds <= 0:
        raise ValueError("cancel_after_seconds must be positive")
    if config.cancel_retry_count < 1:
        raise ValueError("cancel_retry_count must be at least 1")
    if config.cancel_retry_base_seconds < 0:
        raise ValueError("cancel_retry_base_seconds cannot be negative")
    if config.console_enabled:
        try:
            is_loopback = ip_address(config.console_host).is_loopback
        except ValueError as error:
            raise ValueError("console_host must be a numeric loopback address") from error
        if not is_loopback:
            raise ValueError("console_host must be a loopback address")
        if not 1 <= config.console_port <= 65535:
            raise ValueError("console_port must be between 1 and 65535")
    if config.strategy.quote_size <= 0:
        raise ValueError("strategy.quote_size must be positive")
    if config.strategy.min_edge_ticks < 0:
        raise ValueError("strategy.min_edge_ticks cannot be negative")
    if not Decimal() <= config.strategy.min_spread <= config.strategy.max_spread:
        raise ValueError("strategy spread limits are invalid")
    if (
        config.risk.max_order_size <= 0
        or config.risk.max_position_per_token <= 0
        or config.risk.max_total_open_notional <= 0
    ):
        raise ValueError("risk limits must be positive")
    if config.risk.max_open_orders_per_token < 1:
        raise ValueError("risk.max_open_orders_per_token must be at least 1")
    if any(
        market.quote_size is not None and market.quote_size <= 0
        for market in config.enabled_markets
    ):
        raise ValueError("market quote_size must be positive")
    return config


def _decimal_fields(raw: dict, names: set[str]) -> dict:
    result = dict(raw)
    for name in names:
        if result.get(name) is not None:
            result[name] = Decimal(str(result[name]))
    return result


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def update_dotenv_values(path: str | Path, updates: dict[str, str]) -> None:
    """Update selected dotenv values without exposing or discarding unrelated settings."""
    env_path = Path(path)
    for key, value in updates.items():
        if not key or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in key):
            raise ValueError(f"Invalid environment variable name: {key}")
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"Invalid value for {key}")

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(updates)
    result: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            result.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            result.append(f"{key}={remaining.pop(key)}")
        else:
            result.append(line)
    result.extend(f"{key}={value}" for key, value in remaining.items())
    env_path.write_text("\n".join(result) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
