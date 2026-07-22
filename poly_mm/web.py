from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
from collections import deque
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from poly_mm.client import PolymarketClient
from poly_mm.config import BotConfig, MarketConfig, Settings, load_config, update_dotenv_values, write_config
from poly_mm.console import ConsoleServer
from poly_mm.discovery import market_options, resolve_market
from poly_mm.engine import MarketMakerEngine
from poly_mm.models import PreflightReport

logger = logging.getLogger("poly-mm")
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
ACCOUNT_ENV_KEYS = {
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_FUNDER_ADDRESS",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
}
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(POLYMARKET_(?:PRIVATE_KEY|API_KEY|API_SECRET|API_PASSPHRASE)|"
    r"private[_ -]?key|api[_ -]?(?:key|secret)|passphrase)\b(\s*[:=]\s*)([^\s,;]+)"
)
PRIVATE_KEY_PATTERN = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")


class MemoryLogHandler(logging.Handler):
    """Bounded, redacted in-memory log stream for the loopback web console."""

    def __init__(self, maximum: int = 300) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=maximum)
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            line = SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2[REDACTED]", line)
            line = PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", line)
            self.lines.append(line)
        except Exception:  # pragma: no cover - logging must never interrupt trading
            self.handleError(record)


class DashboardController:
    """Keep the web console alive while starting and stopping trading tasks."""

    def __init__(self, config_path: str | Path, env_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.env_path = Path(env_path)
        self.engine: MarketMakerEngine | None = None
        self.task: asyncio.Task[None] | None = None
        self.last_error = ""
        self.last_preflight: PreflightReport | None = None
        self.log_handler = MemoryLogHandler()
        self.memory_logging_enabled = False
        self.lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def settings(self) -> Settings:
        return Settings.from_env(self.env_path)

    def enable_memory_logging(self) -> None:
        if self.memory_logging_enabled:
            return
        logging.getLogger("poly-mm").addHandler(self.log_handler)
        self.memory_logging_enabled = True

    def disable_memory_logging(self) -> None:
        if not self.memory_logging_enabled:
            return
        logging.getLogger("poly-mm").removeHandler(self.log_handler)
        self.memory_logging_enabled = False

    def log_lines(self) -> list[str]:
        return list(self.log_handler.lines)

    def account_status(self) -> dict[str, Any]:
        settings = self.settings()
        signer_address = ""
        account_error = ""
        if settings.private_key:
            try:
                signer_address = PolymarketClient(settings, dry_run=False).signer_address()
            except RuntimeError as error:
                account_error = str(error)
        funder_address = settings.funder or signer_address
        credentials = (
            settings.api_key,
            settings.api_secret,
            settings.api_passphrase,
        )
        credentials_set = all(credentials)
        credentials_partial = any(credentials) and not credentials_set
        funder_ready = settings.signature_type == 0 or bool(settings.funder)
        return {
            "private_key_set": bool(settings.private_key),
            "api_credentials_set": credentials_set,
            "api_credentials_partial": credentials_partial,
            "signature_type": settings.signature_type,
            "signer_address": signer_address,
            "funder_address": funder_address,
            "funder_configured": bool(settings.funder),
            "ready": bool(settings.private_key)
            and credentials_set
            and funder_ready
            and not account_error,
            "error": account_error,
        }

    async def snapshot(self) -> dict[str, Any]:
        config = load_config(self.config_path, require_markets=False)
        if self.engine is not None:
            status = await self.engine.snapshot()
        else:
            status = self._stopped_snapshot(config)
        status["running"] = self.running
        status["account"] = self.account_status()
        status["configuration"] = {
            "market_count": len(config.enabled_markets),
            "cancel_after_seconds": config.cancel_after_seconds,
            "run_duration_seconds": config.run_duration_seconds,
            "max_order_size": str(config.risk.max_order_size),
            "max_position_per_token": str(config.risk.max_position_per_token),
            "max_total_open_notional": str(config.risk.max_total_open_notional),
            "halt_on_fill": config.halt_on_fill,
            "markets": [
                {
                    "url": market.url,
                    "outcome": market.outcome,
                    "market_slug": market.market_slug,
                    "token_id": market.token_id,
                    "condition_id": market.condition_id,
                    "label": market.label,
                    "quote_size": str(market.quote_size or config.strategy.quote_size),
                }
                for market in config.enabled_markets
            ],
        }
        if self.last_error:
            status["last_error"] = self.last_error
            if not self.running:
                status["phase"] = "error"
        if status.get("preflight") is None and self.last_preflight is not None:
            status["preflight"] = _preflight_dict(self.last_preflight)
        return status

    async def start_bot(self) -> dict[str, Any]:
        async with self.lock:
            if self.running:
                raise ValueError("挂单任务已经在运行。")
            config = await self._resolved_config()
            if not config.enabled_markets:
                raise ValueError("请先在 config.toml 中添加至少一个启用的市场。")
            settings = self.settings()
            if not config.dry_run and not settings.private_key:
                raise ValueError("请先在账户设置中保存钱包私钥。")
            client = PolymarketClient(settings, dry_run=config.dry_run)
            self.engine = MarketMakerEngine(config, client)
            self.last_error = ""
            self.last_preflight = None
            self.task = asyncio.create_task(self._run_engine(self.engine))
        await asyncio.sleep(0)
        return {"message": "挂单任务已启动，正在执行启动检查。", "status": await self.snapshot()}

    async def _run_engine(self, engine: MarketMakerEngine) -> None:
        try:
            await engine.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self.last_error = str(error)
            logger.exception("Polymarket maker stopped: %s", error)

    async def stop_bot(self) -> dict[str, Any]:
        if not self.running or self.engine is None:
            raise ValueError("挂单任务当前没有运行。")
        self.engine.request_stop()
        return {"message": "正在停止任务并撤销机器人挂单。", "status": await self.snapshot()}

    async def shutdown(self) -> None:
        if self.running and self.engine is not None:
            self.engine.request_stop()
        if self.task is not None and not self.task.done():
            try:
                await asyncio.wait_for(self.task, timeout=55)
            except TimeoutError:
                logger.error("Timed out waiting for the maker task to stop")

    async def pause_quotes(self) -> dict[str, Any]:
        return await self._active_engine().pause_quotes()

    async def resume_quotes(self) -> dict[str, Any]:
        return await self._active_engine().resume_quotes()

    async def set_quote_expiry(self, hours: object, minutes: object) -> dict[str, Any]:
        return await self._active_engine().set_quote_expiry(hours, minutes)

    async def clear_quote_expiry(self) -> dict[str, Any]:
        return await self._active_engine().clear_quote_expiry()

    async def run_preflight(self) -> dict[str, Any]:
        if self.running:
            raise ValueError("请先停止挂单任务，再单独运行预检。")
        config = await self._resolved_config()
        client = PolymarketClient(self.settings(), dry_run=False)
        report = await asyncio.to_thread(client.run_preflight, replace(config, dry_run=False))
        self.last_preflight = report
        self.last_error = ""
        return {"message": "实盘预检通过。", "status": await self.snapshot()}

    async def save_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            if self.running:
                raise ValueError("请先停止挂单任务，再修改账户设置。")
            current = self.settings()
            private_key = _payload_text(payload, "private_key", 256) or current.private_key or ""
            if not private_key:
                raise ValueError("钱包私钥不能为空。")
            try:
                signature_type = int(payload.get("signature_type", current.signature_type))
            except (TypeError, ValueError) as error:
                raise ValueError("签名类型无效。") from error
            if signature_type not in {0, 1, 2, 3}:
                raise ValueError("签名类型必须是 0、1、2 或 3。")

            requested_funder = _payload_text(payload, "funder_address", 80)
            validation_settings = replace(
                current,
                private_key=private_key,
                funder=requested_funder or current.funder,
                signature_type=signature_type,
                api_key=None,
                api_secret=None,
                api_passphrase=None,
            )
            client = PolymarketClient(validation_settings, dry_run=False)
            signer_address = client.signer_address()
            if signature_type == 0:
                if requested_funder and requested_funder.casefold() != signer_address.casefold():
                    raise ValueError("EOA 类型 0 的资金地址必须与私钥导出的地址一致。")
                funder_address = ""
                validation_settings = replace(validation_settings, funder=None)
                client = PolymarketClient(validation_settings, dry_run=False)
            else:
                funder_address = requested_funder or current.funder or ""
                if not ADDRESS_PATTERN.fullmatch(funder_address):
                    raise ValueError("该签名类型需要填写有效的 0x 资金钱包地址。")
                validation_settings = replace(validation_settings, funder=funder_address)
                client = PolymarketClient(validation_settings, dry_run=False)

            try:
                credentials = await asyncio.to_thread(client.api_credentials)
            except Exception as error:  # noqa: BLE001
                logger.warning("Unable to derive Polymarket API credentials")
                raise ValueError(
                    "无法使用该钱包派生 Polymarket API 凭据，请检查私钥、钱包类型和网络。"
                ) from error

            updates = {
                "POLYMARKET_PRIVATE_KEY": private_key,
                "POLYMARKET_FUNDER_ADDRESS": funder_address,
                "POLYMARKET_SIGNATURE_TYPE": str(signature_type),
                "POLYMARKET_API_KEY": str(credentials["apiKey"]),
                "POLYMARKET_API_SECRET": str(credentials["secret"]),
                "POLYMARKET_API_PASSPHRASE": str(credentials["passphrase"]),
            }
            update_dotenv_values(self.env_path, updates)
            for key in ACCOUNT_ENV_KEYS:
                os.environ[key] = updates[key]
            self.last_error = ""
            self.last_preflight = None

        return {
            "message": "账户设置已保存，并已通过官方 SDK 派生 L2 API 凭据。",
            "status": await self.snapshot(),
        }

    async def resolve_market_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = _payload_text(payload, "url", 500)
        if not url:
            raise ValueError("请先粘贴 Polymarket 市场网址。")
        options = await asyncio.to_thread(market_options, url)
        return {"message": f"识别到 {len(options)} 个可挂单市场。", "markets": options}

    async def save_setup(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            if self.running:
                raise ValueError("请先停止挂单任务，再修改挂单设置。")
            current = load_config(self.config_path, require_markets=False)
            rows = payload.get("markets", [])
            if not isinstance(rows, list) or len(rows) > 20:
                raise ValueError("挂单市场列表无效或超过 20 个。")

            markets: list[MarketConfig] = []
            quote_sizes: list[Decimal] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("挂单市场配置无效。")
                url = _payload_text(row, "url", 500)
                if not url:
                    continue
                outcome = _payload_text(row, "outcome", 100)
                market_slug = _payload_text(row, "market_slug", 250)
                quote_size = _positive_decimal(row.get("quote_size"), "单次挂单数量")
                resolved = await asyncio.to_thread(
                    resolve_market,
                    MarketConfig(
                        url=url,
                        outcome=outcome,
                        market_slug=market_slug,
                        quote_size=quote_size,
                    ),
                )
                markets.append(resolved)
                quote_sizes.append(quote_size)

            cancel_after_seconds = float(
                _positive_decimal(payload.get("cancel_after_seconds"), "撤单等待秒数")
            )
            max_position = _positive_decimal(
                payload.get("max_position_per_token"), "单 outcome 最大仓位"
            )
            max_notional = _positive_decimal(
                payload.get("max_total_open_notional"), "总开放名义金额"
            )
            duration_enabled = payload.get("run_duration_enabled") is True
            hours = _bounded_int(payload.get("run_duration_hours", 0), "有效期小时", 0, 168)
            minutes = _bounded_int(payload.get("run_duration_minutes", 0), "有效期分钟", 0, 59)
            run_duration_seconds = hours * 3600 + minutes * 60 if duration_enabled else 0
            if duration_enabled and run_duration_seconds <= 0:
                raise ValueError("启用有效期时，小时和分钟不能同时为 0。")
            if run_duration_seconds > 7 * 24 * 60 * 60:
                raise ValueError("挂单有效期不能超过 7 天。")
            maximum_quote_size = max(quote_sizes, default=current.strategy.quote_size)
            updated = replace(
                current,
                dry_run=payload.get("dry_run") is True,
                cancel_after_seconds=cancel_after_seconds,
                run_duration_seconds=run_duration_seconds,
                markets=markets,
                strategy=replace(current.strategy, quote_size=maximum_quote_size),
                risk=replace(
                    current.risk,
                    max_order_size=maximum_quote_size,
                    max_position_per_token=max_position,
                    max_total_open_notional=max_notional,
                ),
            )
            write_config(self.config_path, updated)
            self.last_error = ""
            self.last_preflight = None
        message = "挂单设置已保存。" if markets else "挂单设置已清空；当前没有挂单任务。"
        return {"message": message, "status": await self.snapshot()}

    async def _resolved_config(self) -> BotConfig:
        config = load_config(self.config_path, require_markets=False)
        resolved_markets = []
        for market in config.markets:
            resolved = await asyncio.to_thread(resolve_market, market) if market.enabled else market
            resolved_markets.append(resolved)
        return replace(config, markets=resolved_markets)

    def _active_engine(self) -> MarketMakerEngine:
        if not self.running or self.engine is None:
            raise ValueError("请先启动挂单任务。")
        return self.engine

    def _stopped_snapshot(self, config: BotConfig) -> dict[str, Any]:
        return {
            "phase": "stopped",
            "dry_run": config.dry_run,
            "paused": True,
            "websocket_connected": False,
            "markets": [
                {
                    "label": market.label or market.outcome,
                    "token_id": market.token_id,
                    "condition_id": market.condition_id,
                    "position": "0",
                    "book": {"best_bid": None, "best_ask": None, "spread": None},
                    "halted": False,
                }
                for market in config.enabled_markets
            ],
            "orders": [],
            "quote_task": {
                "deadline_at": None,
                "remaining_seconds": None,
                "expired": False,
            },
            "preflight": None,
            "last_error": "",
        }


def _payload_text(payload: dict[str, Any], key: str, maximum: int) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是文本。")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{key} 过长。")
    return value


def _positive_decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{label}必须是有效数字。") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label}必须大于 0。")
    return result


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}无效。") from error
    if not minimum <= result <= maximum:
        raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间。")
    return result


def _preflight_dict(report: PreflightReport) -> dict[str, str]:
    return {
        "signer_address": report.signer_address,
        "funder_address": report.funder_address,
        "collateral_balance": str(report.collateral_balance),
        "minimum_allowance": str(report.minimum_allowance),
        "country": report.country,
        "region": report.region,
    }


async def async_main() -> None:
    args = parse_args()
    config = load_config(args.config, require_markets=False)
    controller = DashboardController(args.config, args.env)
    controller.enable_memory_logging()
    console = ConsoleServer(
        controller,
        host=config.console_host,
        port=config.console_port,
        enabled=config.console_enabled,
    )
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for name in ("SIGINT", "SIGTERM"):
        loop.add_signal_handler(getattr(signal, name), stop_event.set)
    console.start(loop)
    try:
        await stop_event.wait()
    finally:
        await controller.shutdown()
        console.stop()
        controller.disable_memory_logging()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket maker web controller")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--env", default=".env")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
