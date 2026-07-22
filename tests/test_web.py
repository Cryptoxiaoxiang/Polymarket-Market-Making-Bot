import asyncio
import logging
import os
from dataclasses import replace
from decimal import Decimal

from poly_mm.config import MarketConfig, load_config
from poly_mm.web import ACCOUNT_ENV_KEYS, DashboardController, MemoryLogHandler


def test_memory_log_handler_is_bounded_and_redacts_secrets() -> None:
    handler = MemoryLogHandler(maximum=2)
    logger = logging.getLogger("poly-mm-test-memory")
    logger.handlers = [handler]
    logger.propagate = False
    private_key = "0x" + "a" * 64

    logger.info("first")
    logger.warning("POLYMARKET_API_SECRET=secret-value")
    logger.error("signing material %s", private_key)

    lines = list(handler.lines)
    assert len(lines) == 2
    assert "first" not in "\n".join(lines)
    assert "secret-value" not in "\n".join(lines)
    assert private_key not in "\n".join(lines)
    assert lines[-1].endswith("signing material [REDACTED_PRIVATE_KEY]")


def test_web_controller_supports_zero_markets_but_refuses_task_start(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("dry_run = true\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    controller = DashboardController(config_path, env_path)

    async def scenario() -> None:
        status = await controller.snapshot()
        assert status["markets"] == []
        assert status["configuration"]["market_count"] == 0
        try:
            await controller.start_bot()
        except ValueError as error:
            assert "添加至少一个启用的市场" in str(error)
        else:
            raise AssertionError("expected task start without markets to fail")

    asyncio.run(scenario())


def test_web_account_save_derives_credentials_without_returning_secrets(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'dry_run = false\n[[markets]]\ntoken_id = "token-1"\noutcome = "Yes"\n',
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "POLYMARKET_PRIVATE_KEY=\n"
        "POLYMARKET_FUNDER_ADDRESS=\n"
        "POLYMARKET_SIGNATURE_TYPE=0\n"
        "POLYMARKET_API_KEY=\n"
        "POLYMARKET_API_SECRET=\n"
        "POLYMARKET_API_PASSPHRASE=\n"
        "POLYMARKET_ORDER_JOURNAL_PATH=/tmp/orders.json\n",
        encoding="utf-8",
    )
    for key in ACCOUNT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "poly_mm.web.PolymarketClient.api_credentials",
        lambda self: {
            "apiKey": "derived-key",
            "secret": "derived-secret",
            "passphrase": "derived-passphrase",
        },
    )

    controller = DashboardController(config_path, env_path)
    private_key = "0x" + "1" * 64
    result = asyncio.run(
        controller.save_account(
            {
                "private_key": private_key,
                "signature_type": 0,
                "funder_address": "",
            }
        )
    )

    account = result["status"]["account"]
    assert account["private_key_set"] is True
    assert account["api_credentials_set"] is True
    assert account["signature_type"] == 0
    assert account["funder_address"] == account["signer_address"]
    assert private_key not in str(result)
    assert "derived-secret" not in str(result)
    text = env_path.read_text(encoding="utf-8")
    assert f"POLYMARKET_PRIVATE_KEY={private_key}" in text
    assert "POLYMARKET_API_SECRET=derived-secret" in text
    assert "POLYMARKET_ORDER_JOURNAL_PATH=/tmp/orders.json" in text
    assert os.stat(env_path).st_mode & 0o777 == 0o600


def test_eoa_account_rejects_a_different_funder_address(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[markets]]\ntoken_id = "token-1"\n',
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    for key in ACCOUNT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    controller = DashboardController(config_path, env_path)

    async def scenario() -> None:
        try:
            await controller.save_account(
                {
                    "private_key": "0x" + "2" * 64,
                    "signature_type": 0,
                    "funder_address": "0x" + "3" * 40,
                }
            )
        except ValueError as error:
            assert "必须与私钥导出的地址一致" in str(error)
        else:
            raise AssertionError("expected a mismatched EOA funder to be rejected")

    asyncio.run(scenario())


def test_web_setup_is_explicitly_saved_and_can_be_cleared(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "dry_run = false\n[risk]\nmax_position_per_token = \"25\"\n"
        "max_total_open_notional = \"100\"\n",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")

    def fake_resolve(market: MarketConfig) -> MarketConfig:
        return replace(
            market,
            token_id="123",
            condition_id="0xabc",
            market_slug="resolved-market",
            label=f"Resolved question — {market.outcome}",
        )

    monkeypatch.setattr("poly_mm.web.resolve_market", fake_resolve)
    controller = DashboardController(config_path, env_path)
    payload = {
        "markets": [
            {
                "url": "https://polymarket.com/event/example",
                "outcome": "Yes",
                "market_slug": "resolved-market",
                "quote_size": "2.5",
            }
        ],
        "max_position_per_token": "12",
        "max_total_open_notional": "40",
        "cancel_after_seconds": "8",
        "run_duration_enabled": True,
        "run_duration_hours": 1,
        "run_duration_minutes": 30,
        "dry_run": False,
    }

    saved = asyncio.run(controller.save_setup(payload))
    config = load_config(config_path)
    assert saved["status"]["configuration"]["market_count"] == 1
    assert config.markets[0].token_id == "123"
    assert config.markets[0].quote_size == Decimal("2.5")
    assert config.risk.max_order_size == Decimal("2.5")
    assert config.run_duration_seconds == 5_400
    assert config.dry_run is False

    payload["markets"] = []
    payload["run_duration_enabled"] = False
    cleared = asyncio.run(controller.save_setup(payload))
    assert cleared["status"]["configuration"]["market_count"] == 0
    assert load_config(config_path, require_markets=False).markets == []
