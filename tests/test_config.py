import pytest

from poly_mm.config import load_config, update_dotenv_values


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
