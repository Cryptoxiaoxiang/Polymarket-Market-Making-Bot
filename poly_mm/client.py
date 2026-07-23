from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from time import time
from uuid import uuid4

import requests

from poly_mm.config import BotConfig, Settings
from poly_mm.models import Level, ManagedOrder, OrderBook, PreflightReport, Quote, Side

logger = logging.getLogger("poly-mm")
COLLATERAL_SCALE = Decimal(10**6)
# Polymarket documents Japan as restricted in its frontend UI only; CLOB API
# trading is not restricted there. The geoblock endpoint still reports the IP
# as blocked, so preflight must distinguish it from API-blocked jurisdictions.
# https://docs.polymarket.com/api-reference/geoblock
FRONTEND_ONLY_RESTRICTED_COUNTRIES = frozenset({"JP"})


class PolymarketClient:
    def __init__(self, settings: Settings, dry_run: bool) -> None:
        self.settings, self.dry_run = settings, dry_run
        self._dry_orders: dict[str, ManagedOrder] = {}
        self._prepared_orders: dict[str, object] = {}
        self._sdk = None
        self.websocket_connected = False

    def signer_address(self) -> str:
        if not self.settings.private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is required for live trading")
        try:
            from eth_account import Account

            return Account.from_key(self.settings.private_key).address
        except (TypeError, ValueError) as error:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is not a valid EVM private key") from error

    def funder_address(self) -> str:
        return self.settings.funder or self.signer_address()

    def check_geoblock(self) -> dict:
        response = requests.get(self.settings.geoblock_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "blocked" not in data:
            raise RuntimeError("Polymarket returned an invalid geoblock response")
        return data

    def run_preflight(self, config: BotConfig) -> PreflightReport:
        if self.dry_run:
            raise RuntimeError("Live preflight is only available when dry_run=false")
        if self.settings.signature_type not in {0, 1, 2, 3}:
            raise RuntimeError("POLYMARKET_SIGNATURE_TYPE must be 0, 1, 2, or 3")

        signer = self.signer_address()
        funder = self.funder_address()
        if self.settings.signature_type == 0 and signer.casefold() != funder.casefold():
            raise RuntimeError(
                "EOA signature type 0 requires POLYMARKET_FUNDER_ADDRESS to match "
                "the private-key address"
            )

        geo = self.check_geoblock()
        country = str(geo.get("country") or "").upper()
        if bool(geo.get("blocked")) and country not in FRONTEND_ONLY_RESTRICTED_COUNTRIES:
            location = "/".join(
                value
                for value in [
                    country,
                    str(geo.get("region") or ""),
                ]
                if value
            )
            raise RuntimeError(
                f"Polymarket trading is blocked for this VPS IP ({location or 'unknown'})"
            )
        if bool(geo.get("blocked")):
            logger.warning(
                "Polymarket frontend UI is restricted at location %s/%s; "
                "continuing CLOB API preflight because the API is not restricted there",
                country,
                str(geo.get("region") or ""),
            )

        sdk = self._authenticated_sdk()
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=self.settings.signature_type,
        )
        sdk.update_balance_allowance(params)
        raw = sdk.get_balance_allowance(params)
        balance = _collateral_amount(raw.get("balance", "0"))
        allowances = _allowance_amounts(raw)
        if not allowances:
            raise RuntimeError("CLOB returned no collateral allowance values")
        minimum_allowance = min(allowances)

        # Confirm L2 credentials can read account state before the engine can submit.
        sdk.get_open_orders(only_first_page=True)
        return PreflightReport(
            signer_address=signer,
            funder_address=funder,
            collateral_balance=balance,
            minimum_allowance=minimum_allowance,
            country=country,
            region=str(geo.get("region") or ""),
        )

    def get_orderbook(self, token_id: str) -> OrderBook:
        response = requests.get(f"{self.settings.host}/book", params={"token_id": token_id}, timeout=10)
        response.raise_for_status()
        data = response.json()
        bids = [
            Level(Decimal(row["price"]), Decimal(row["size"]))
            for row in data.get("bids", [])
        ]
        asks = [
            Level(Decimal(row["price"]), Decimal(row["size"]))
            for row in data.get("asks", [])
        ]
        # Do not rely on transport ordering. Live CLOB responses have been
        # observed with bids ascending and asks descending even though the
        # documentation examples show best-price-first arrays.
        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)
        return OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            tick_size=Decimal(str(data["tick_size"])),
            min_order_size=Decimal(str(data["min_order_size"])),
            neg_risk=bool(data.get("neg_risk", False)),
        )

    def create_order(
        self,
        quote: Quote,
        *,
        post_only: bool = True,
        tick_size: Decimal | None = None,
        submission_key: str | None = None,
    ) -> ManagedOrder:
        if self.dry_run:
            order = ManagedOrder(f"dry-{uuid4().hex[:12]}", quote, time())
            self._dry_orders[order.order_id] = order
            logger.info(
                "DRY-RUN would post %s %s shares @ %s (%s), post_only=%s",
                quote.side.value,
                quote.size,
                quote.price,
                quote.token_id,
                post_only,
            )
            return order
        sdk = self._authenticated_sdk()
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY, SELL
        resolved_tick_size = tick_size or self.get_orderbook(quote.token_id).tick_size
        order_args = OrderArgs(
            token_id=quote.token_id,
            price=float(quote.price),
            size=float(quote.size),
            side=BUY if quote.side == Side.BUY else SELL,
        )
        options = PartialCreateOrderOptions(
            tick_size=str(resolved_tick_size), neg_risk=quote.neg_risk
        )
        if submission_key:
            # A POST can reach the CLOB even when its HTTP response is lost.
            # Re-post the exact same signed order on retry so its order hash
            # remains stable instead of reserving shares a second time.
            prepared = self._prepared_orders.get(submission_key)
            if prepared is None:
                prepared = sdk.create_order(order_args=order_args, options=options)
                self._prepared_orders[submission_key] = prepared
            result = sdk.post_order(
                prepared,
                order_type=OrderType.GTC,
                post_only=post_only,
            )
        else:
            result = sdk.create_and_post_order(
                order_args=order_args,
                options=options,
                order_type=OrderType.GTC,
                post_only=post_only,
            )
        if result.get("success") is False:
            raise RuntimeError(f"CLOB rejected order: {result.get('errorMsg') or result}")
        order_id = str(result.get("orderID") or result.get("order_id") or result.get("id") or "")
        if not order_id:
            raise RuntimeError(f"CLOB did not return an order ID: {result}")
        if submission_key:
            self._prepared_orders.pop(submission_key, None)
        return ManagedOrder(order_id, quote, time())

    def discard_prepared_order(self, submission_key: str) -> None:
        self._prepared_orders.pop(submission_key, None)

    def sync_conditional_allowance(self, token_id: str) -> None:
        """Refresh CLOB's conditional-token balance cache before a SELL."""
        if self.dry_run:
            return
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        self._authenticated_sdk().update_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=self.settings.signature_type,
            )
        )

    def get_conditional_balance(self, token_id: str) -> Decimal:
        """Return the currently available conditional-token shares."""
        if self.dry_run:
            return Decimal()
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        raw = self._authenticated_sdk().get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=self.settings.signature_type,
            )
        )
        return Decimal(str(raw.get("balance") or "0")) / COLLATERAL_SCALE

    def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            self._dry_orders.pop(order_id, None)
            logger.info("DRY-RUN would cancel %s", order_id)
            return
        from py_clob_client_v2 import OrderPayload

        self._authenticated_sdk().cancel_order(OrderPayload(orderID=order_id))

    def cancel_market_orders(self, condition_id: str, token_id: str) -> dict:
        if self.dry_run:
            for order_id, order in list(self._dry_orders.items()):
                if order.quote.token_id == token_id:
                    self._dry_orders.pop(order_id, None)
            logger.info("DRY-RUN would cancel configured token %s", token_id)
            return {}
        from py_clob_client_v2 import OrderMarketCancelParams

        return self._authenticated_sdk().cancel_market_orders(
            OrderMarketCancelParams(market=condition_id, asset_id=token_id)
        )

    def get_order(self, order_id: str) -> dict:
        """Get the CLOB's current record for an order we created."""
        if self.dry_run:
            return {"id": order_id, "status": "ORDER_STATUS_LIVE", "size_matched": "0"}
        return self._authenticated_sdk().get_order(order_id)

    def get_open_orders(self, token_id: str | None = None) -> list[dict]:
        if self.dry_run:
            return [
                {"id": order.order_id, "asset_id": order.quote.token_id}
                for order in self._dry_orders.values()
                if token_id is None or order.quote.token_id == token_id
            ]
        from py_clob_client_v2 import OpenOrderParams

        params = OpenOrderParams(asset_id=token_id) if token_id else None
        return self._authenticated_sdk().get_open_orders(params)

    def get_order_matched_shares(
        self, order_id: str, token_id: str, created_at: float | None = None
    ) -> Decimal:
        """Recover an order's matched shares from authenticated trade history."""
        if self.dry_run:
            return Decimal()
        from py_clob_client_v2 import TradeParams

        after = max(0, int(created_at) - 60) if created_at is not None else None
        trades = self._authenticated_sdk().get_trades(
            TradeParams(asset_id=token_id, after=after), only_first_page=True
        )
        matched = Decimal()
        seen_trade_ids: set[str] = set()
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            status = str(trade.get("status") or "").upper()
            if status.endswith("FAILED"):
                continue
            trade_id = str(trade.get("id") or "")
            if trade_id and trade_id in seen_trade_ids:
                continue
            if trade_id:
                seen_trade_ids.add(trade_id)
            if str(trade.get("taker_order_id") or "") == order_id:
                matched += Decimal(str(trade.get("size") or "0"))
                continue
            for maker_order in trade.get("maker_orders") or []:
                if not isinstance(maker_order, dict):
                    continue
                if str(maker_order.get("order_id") or "") == order_id:
                    matched += Decimal(str(maker_order.get("matched_amount") or "0"))
        return matched

    def get_positions(self, condition_ids: list[str] | None = None) -> dict[str, Decimal]:
        if self.dry_run:
            return {}
        params: dict[str, str | int] = {
            "user": self.funder_address(),
            "sizeThreshold": "0",
            "limit": 500,
        }
        condition_ids = [condition_id for condition_id in (condition_ids or []) if condition_id]
        if condition_ids:
            params["market"] = ",".join(condition_ids)
        response = requests.get(
            f"{self.settings.data_api_url.rstrip('/')}/positions", params=params, timeout=10
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError("Polymarket Data API returned an invalid positions response")
        positions: dict[str, Decimal] = {}
        for row in rows:
            token_id = str(row.get("asset") or "")
            if token_id:
                positions[token_id] = positions.get(token_id, Decimal()) + Decimal(
                    str(row.get("size") or "0")
                )
        return positions

    def api_credentials(self) -> dict[str, str]:
        sdk = self._authenticated_sdk()
        return {
            "apiKey": sdk.creds.api_key,
            "secret": sdk.creds.api_secret,
            "passphrase": sdk.creds.api_passphrase,
        }

    async def stream_user_events(self, condition_ids: list[str]):
        if self.dry_run:
            return
        from websockets.asyncio.client import connect

        credentials = self.api_credentials()
        self.websocket_connected = False
        try:
            async with connect(self.settings.user_ws_url, ping_interval=None) as websocket:
                await websocket.send(
                    json.dumps(
                        {"auth": credentials, "markets": condition_ids, "type": "user"},
                        separators=(",", ":"),
                    )
                )
                self.websocket_connected = True
                heartbeat = asyncio.create_task(self._user_ws_heartbeat(websocket))
                try:
                    async for raw_message in websocket:
                        if raw_message == "PONG":
                            continue
                        message = json.loads(raw_message)
                        if isinstance(message, list):
                            for item in message:
                                if isinstance(item, dict):
                                    yield item
                        elif isinstance(message, dict):
                            yield message
                finally:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
        finally:
            self.websocket_connected = False

    @staticmethod
    async def _user_ws_heartbeat(websocket) -> None:
        while True:
            await asyncio.sleep(10)
            await websocket.send("PING")

    def _authenticated_sdk(self):
        if self._sdk is not None:
            return self._sdk
        if not self.settings.private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is required for live trading")
        from py_clob_client_v2 import ApiCreds, ClobClient

        provided_credentials = (
            self.settings.api_key,
            self.settings.api_secret,
            self.settings.api_passphrase,
        )
        if any(provided_credentials) and not all(provided_credentials):
            raise RuntimeError(
                "Provide all three POLYMARKET_API_KEY/SECRET/PASSPHRASE values or leave all blank"
            )
        credentials = None
        if self.settings.api_key and self.settings.api_secret and self.settings.api_passphrase:
            credentials = ApiCreds(
                api_key=self.settings.api_key,
                api_secret=self.settings.api_secret,
                api_passphrase=self.settings.api_passphrase,
            )
        base = ClobClient(
            host=self.settings.host,
            chain_id=self.settings.chain_id,
            key=self.settings.private_key,
            signature_type=self.settings.signature_type,
            funder=self.funder_address(),
            # The VPS clock is NTP-synchronised. Asking /time before every L1/L2
            # request doubles the network round trips on the hot path.
            use_server_time=False,
        )
        credentials = credentials or base.create_or_derive_api_key()
        self._sdk = ClobClient(
            host=self.settings.host,
            chain_id=self.settings.chain_id,
            key=self.settings.private_key,
            creds=credentials,
            signature_type=self.settings.signature_type,
            funder=self.funder_address(),
            use_server_time=False,
        )
        return self._sdk


def _collateral_amount(raw: object) -> Decimal:
    return Decimal(str(raw or "0")) / COLLATERAL_SCALE


def _allowance_amounts(raw: dict) -> list[Decimal]:
    values: list[object] = []
    allowances = raw.get("allowances")
    if isinstance(allowances, dict):
        values.extend(allowances.values())
    elif isinstance(allowances, list):
        values.extend(allowances)
    if raw.get("allowance") is not None:
        values.append(raw["allowance"])
    return [_collateral_amount(value) for value in values]
