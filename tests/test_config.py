import pytest

from poly_mm.config import load_config


def test_example_config_defaults_to_live_trading() -> None:
    config = load_config("config.example.toml")

    assert config.dry_run is False
    assert config.preflight_enabled is True
    assert str(config.risk.max_total_open_notional) == "5"


def test_console_rejects_non_loopback_bind(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'console_host = "0.0.0.0"\n[[markets]]\ntoken_id = "token-1"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="loopback"):
        load_config(path)
