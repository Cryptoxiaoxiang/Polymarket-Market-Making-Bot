import pytest

from dataclasses import replace
from decimal import Decimal

from poly_mm.config import BotConfig, MarketConfig, load_config, update_dotenv_values, write_config


def test_example_config_defaults_to_live_trading() -> None:
    config = load_config("config.example.toml", require_markets=False)

    assert config.dry_run is False
    assert config.preflight_enabled is True
    assert str(config.risk.max_total_open_notional) == "5"
    assert config.markets == []


def test_trading_config_requires_an_enabled_market(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("dry_run = true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="At least one enabled market"):
        load_config(path)

    assert load_config(path, require_markets=False).markets == []


def test_console_rejects_non_loopback_bind(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'console_host = "0.0.0.0"\n[[markets]]\ntoken_id = "token-1"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="loopback"):
        load_config(path)


def test_update_dotenv_values_preserves_unrelated_settings(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("# account\nSECRET=old\nKEEP=value\n", encoding="utf-8")

    update_dotenv_values(path, {"SECRET": "new", "ADDED": "value"})

    assert path.read_text(encoding="utf-8") == (
        "# account\nSECRET=new\nKEEP=value\nADDED=value\n"
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_config_round_trips_web_setup_without_secrets(tmp_path) -> None:
    path = tmp_path / "config.toml"
    config = BotConfig(
        run_duration_seconds=5_400,
        markets=[
            MarketConfig(
                url="https://polymarket.com/event/example",
                outcome="Yes",
                market_slug="example-market",
                token_id="123",
                condition_id="0xabc",
                label='Question "A" — Yes',
                quote_size=Decimal("2.5"),
            )
        ],
    )
    config = replace(
        config,
        risk=replace(config.risk, max_order_size=Decimal("2.5")),
    )

    write_config(path, config)
    loaded = load_config(path)

    assert loaded.run_duration_seconds == 5_400
    assert loaded.markets == config.markets
    assert loaded.risk.max_order_size == Decimal("2.5")
    assert "PRIVATE_KEY" not in path.read_text(encoding="utf-8")
