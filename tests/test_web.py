import asyncio
import os

from poly_mm.web import ACCOUNT_ENV_KEYS, DashboardController


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
