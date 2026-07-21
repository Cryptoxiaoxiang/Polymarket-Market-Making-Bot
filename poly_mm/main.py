from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import replace

from poly_mm.client import PolymarketClient
from poly_mm.config import Settings, load_config
from poly_mm.console import ConsoleServer
from poly_mm.discovery import resolve_market
from poly_mm.engine import MarketMakerEngine


async def async_main() -> None:
    args = argparse.ArgumentParser(description="Polymarket passive market maker")
    args.add_argument("--config", default="config.toml")
    args.add_argument(
        "--preflight-only",
        action="store_true",
        help="check live credentials, geoblock, pUSD balance, and allowances; never place orders",
    )
    parsed = args.parse_args()
    config = load_config(parsed.config)
    resolved_markets = []
    for market in config.markets:
        resolved = resolve_market(market) if market.enabled else market
        resolved_markets.append(resolved)
        if resolved.enabled:
            logging.getLogger("poly-mm").info(
                "Resolved %s to token %s", resolved.label or resolved.outcome, resolved.token_id
            )
    config = replace(config, markets=resolved_markets)
    settings = Settings.from_env()
    client = PolymarketClient(
        settings,
        dry_run=False if parsed.preflight_only else config.dry_run,
    )
    if parsed.preflight_only:
        report = await asyncio.to_thread(client.run_preflight, config)
        logging.getLogger("poly-mm").info(
            "Preflight passed: signer=%s funder=%s pUSD=%s min_allowance=%s location=%s/%s",
            report.signer_address,
            report.funder_address,
            report.collateral_balance,
            report.minimum_allowance,
            report.country,
            report.region,
        )
        return

    engine = MarketMakerEngine(config, client)
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        loop.add_signal_handler(getattr(signal, name), engine.request_stop)
    console = ConsoleServer(
        engine,
        host=config.console_host,
        port=config.console_port,
        password=settings.console_password,
        enabled=config.console_enabled,
    )
    console.start(loop)
    try:
        await engine.run()
    finally:
        console.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
