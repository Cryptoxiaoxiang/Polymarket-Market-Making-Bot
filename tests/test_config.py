from poly_mm.config import load_config


def test_example_config_defaults_to_live_trading() -> None:
    config = load_config("config.example.toml")

    assert config.dry_run is False
    assert config.preflight_enabled is True
    assert str(config.risk.max_total_open_notional) == "5"
