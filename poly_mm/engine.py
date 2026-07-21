from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from decimal import Decimal
from time import monotonic, time

from poly_mm.client import PolymarketClient
from poly_mm.config import BotConfig
from poly_mm.journal import OrderJournal
from poly_mm.models import ManagedOrder
from poly_mm.risk import RiskManager
from poly_mm.strategy import PassiveMakerStrategy

logger = logging.getLogger("poly-mm")
MAX_QUOTE_DURATION_SECONDS = 7 * 24 * 60 * 60
OPEN_ORDER_STATUSES = {"LIVE", "OPEN", "DELAYED", "UNMATCHED"}
TERMINAL_ORDER_STATUSES = {
    "CANCELED",
    "CANCELLED",
    "CANCELED_MARKET_RESOLVED",
    "INVALID",
    "MATCHED",
    "EXPIRED",
    "FAILED",
}


class MarketMakerEngine:
    def __init__(
        self,
        config: BotConfig,
        client: PolymarketClient,
        journal: OrderJournal | None = None,
    ) -> None:
        self.config, self.client = config, client
        self.strategy, self.risk = PassiveMakerStrategy(config.strategy), RiskManager(config.risk)
        self.journal = journal or OrderJournal(client.settings.order_journal_path)
        self.orders: dict[str, ManagedOrder] = {}
        self.positions: dict[str, Decimal] = {}
        self.halted_tokens: set[str] = set()
        self.stop = asyncio.Event()
        self._user_events: asyncio.Queue[dict] = asyncio.Queue()
        self._websocket_task: asyncio.Task[None] | None = None
        self._next_position_poll_at = 0.0
        self.paused = False
        self.phase = "created"
        self.started_at = time()
        self.last_tick_at: float | None = None
        self.last_error = ""
        self.preflight_report = None
        self.latest_books: dict[str, dict[str, str | bool]] = {}
        self.quote_deadline_at: float | None = None
        self.quote_task_expired = False

    def request_stop(self) -> None:
        self.stop.set()

    async def run(self) -> None:
        logger.info("Starting Polymarket maker: dry_run=%s", self.config.dry_run)
        self.phase = "starting"
        shutdown_failures: list[str] = []
        try:
            self._restore_orders()
            if not self.config.dry_run and self.config.preflight_enabled:
                report = await asyncio.to_thread(self.client.run_preflight, self.config)
                self.preflight_report = report
                logger.info(
                    "Live preflight passed: signer=%s funder=%s pUSD=%s min_allowance=%s location=%s/%s",
                    report.signer_address,
                    report.funder_address,
                    report.collateral_balance,
                    report.minimum_allowance,
                    report.country,
                    report.region,
                )
            elif not self.config.dry_run:
                logger.warning("Live preflight is disabled by configuration")

            if self.config.cancel_all_on_start:
                await self._cancel_configured_orders_on_start()
            else:
                await self._reconcile_orders()

            if not self.config.dry_run and self.config.websocket_enabled:
                self._websocket_task = asyncio.create_task(self._watch_user_events())

            self.phase = "running"
            while not self.stop.is_set():
                await self._drain_user_events()
                await self._tick()
                await self._wait_for_event_or_poll()
        except Exception as error:
            self.phase = "error"
            self.last_error = str(error)
            raise
        finally:
            if self.phase != "error":
                self.phase = "stopping"
            if self._websocket_task is not None:
                self._websocket_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._websocket_task
            if self.config.cancel_all_on_shutdown:
                shutdown_failures = await self._cancel_all_tracked_orders()
            if shutdown_failures:
                self.phase = "error"
                self.last_error = "Unable to confirm cancellation for orders: " + ", ".join(
                    shutdown_failures
                )
                raise RuntimeError(self.last_error)
            if self.phase != "error":
                self.phase = "stopped"
            logger.info("Polymarket maker stopped")

    def _restore_orders(self) -> None:
        # Dry-run must never consume or overwrite the recovery journal that may
        # belong to a live process using the same working directory.
        if self.config.dry_run:
            return
        restored = self.journal.load()
        self.orders = {order.order_id: order for order in restored}
        self.quote_deadline_at = self.journal.load_quote_deadline()
        if self.quote_deadline_at is not None and self.quote_deadline_at <= time():
            self.quote_task_expired = True
            self.paused = True
            logger.warning("Restored quote-task deadline has expired; quoting remains paused")
        if restored:
            logger.warning("Restored %d tracked order(s) from the crash journal", len(restored))

    def _persist_orders(self) -> None:
        if self.config.dry_run:
            return
        self.journal.save(
            list(self.orders.values()),
            quote_deadline_at=self.quote_deadline_at,
        )

    async def _tick(self) -> None:
        self.last_tick_at = time()
        await self._expire_quote_task_if_due()
        await self._reconcile_orders()
        await self._cancel_stale()
        if not await self._refresh_positions_if_due():
            return
        if self.paused:
            return

        active = list(self.orders.values())
        for market in self.config.enabled_markets:
            if market.token_id in self.halted_tokens:
                continue
            if any(order.quote.token_id == market.token_id for order in active):
                continue
            try:
                book = await asyncio.to_thread(self.client.get_orderbook, market.token_id)
                self.latest_books[market.token_id] = {
                    "best_bid": str(book.best_bid.price) if book.best_bid else "",
                    "best_ask": str(book.best_ask.price) if book.best_ask else "",
                    "spread": str(book.spread) if book.spread is not None else "",
                    "tick_size": str(book.tick_size),
                    "neg_risk": book.neg_risk,
                }
                quote = self.strategy.build_quote(market, book)
                if quote and self.risk.approve(quote, active, self.positions):
                    order = await asyncio.to_thread(self.client.create_order, quote)
                    self.orders[order.order_id] = order
                    self._persist_orders()
                    active.append(order)
            except Exception as error:  # keep other markets operating
                logger.warning("Skip %s this cycle: %s", market.label or market.token_id, error)

    async def _refresh_positions_if_due(self) -> bool:
        now = monotonic()
        if now < self._next_position_poll_at:
            return True
        condition_ids = [market.condition_id for market in self.config.enabled_markets]
        try:
            self.positions = await asyncio.to_thread(self.client.get_positions, condition_ids)
        except Exception as error:
            logger.warning("Unable to refresh positions; pausing new quotes: %s", error)
            self._next_position_poll_at = now + 1
            return False
        self._next_position_poll_at = now + self.config.position_poll_interval_seconds
        if self.config.halt_on_fill:
            for market in self.config.enabled_markets:
                size = self.positions.get(market.token_id, Decimal())
                if size > 0 and market.token_id not in self.halted_tokens:
                    self.halted_tokens.add(market.token_id)
                    logger.warning(
                        "Existing position %s detected for %s; token halted",
                        size,
                        market.label or market.token_id,
                    )
                    await self._cancel_token_orders(market.token_id)
        return True

    async def _reconcile_orders(self) -> None:
        """REST reconciliation is the source-of-truth fallback for WebSocket gaps."""
        changed = False
        for order in list(self.orders.values()):
            try:
                state = await asyncio.to_thread(self.client.get_order, order.order_id)
            except Exception as error:
                logger.warning("Unable to reconcile order %s: %s", order.order_id, error)
                try:
                    open_orders = await asyncio.to_thread(
                        self.client.get_open_orders, order.quote.token_id
                    )
                except Exception:
                    continue
                if order.order_id not in {_order_id(item) for item in open_orders}:
                    logger.info(
                        "Order %s is absent from open orders; removing stale journal entry",
                        order.order_id,
                    )
                    self.orders.pop(order.order_id, None)
                    changed = True
                continue
            filled = _matched_shares(state, order)
            if filled > order.filled_size:
                order.filled_size = filled
                changed = True
                await self._handle_fill(order, source="REST")
            status = _normalise_status(state.get("status"))
            if status in TERMINAL_ORDER_STATUSES:
                self.orders.pop(order.order_id, None)
                changed = True
            elif status not in OPEN_ORDER_STATUSES:
                logger.warning(
                    "Order %s returned unknown status %r; retaining it for safety",
                    order.order_id,
                    state.get("status"),
                )
        if changed:
            self._persist_orders()

    async def _cancel_stale(self) -> None:
        for order in list(self.orders.values()):
            if order.age_seconds < self.config.cancel_after_seconds:
                continue
            if await self._cancel_order_reliably(order):
                self.orders.pop(order.order_id, None)
                self._persist_orders()

    async def _cancel_order_reliably(self, order: ManagedOrder) -> bool:
        for attempt in range(self.config.cancel_retry_count):
            try:
                await asyncio.to_thread(self.client.cancel_order, order.order_id)
                open_orders = await asyncio.to_thread(
                    self.client.get_open_orders, order.quote.token_id
                )
                open_ids = {_order_id(item) for item in open_orders}
                if order.order_id not in open_ids:
                    logger.info("Cancellation confirmed for %s", order.order_id)
                    return True
            except Exception as error:
                logger.warning(
                    "Cancel attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self.config.cancel_retry_count,
                    order.order_id,
                    error,
                )
            if attempt + 1 < self.config.cancel_retry_count:
                await asyncio.sleep(self.config.cancel_retry_base_seconds * (2**attempt))
        logger.critical("Cancellation could not be confirmed for %s", order.order_id)
        return False

    async def _cancel_configured_orders_on_start(self) -> None:
        """Cancel every account order on configured tokens, then require an empty book.

        This deliberately includes manual orders on those exact tokens. It closes the
        tiny crash window between CLOB acceptance and local journal persistence.
        """
        for market in self.config.enabled_markets:
            await asyncio.to_thread(
                self.client.cancel_market_orders, market.condition_id, market.token_id
            )
        remaining: list[dict] = []
        for attempt in range(self.config.cancel_retry_count):
            remaining = []
            for market in self.config.enabled_markets:
                remaining.extend(
                    await asyncio.to_thread(self.client.get_open_orders, market.token_id)
                )
            if not remaining:
                self.orders.clear()
                self._persist_orders()
                logger.info("Startup cancellation confirmed for all configured tokens")
                return
            for row in remaining:
                order_id = _order_id(row)
                if order_id:
                    with suppress(Exception):
                        await asyncio.to_thread(self.client.cancel_order, order_id)
            if attempt + 1 < self.config.cancel_retry_count:
                await asyncio.sleep(self.config.cancel_retry_base_seconds * (2**attempt))
        raise RuntimeError(
            "Startup aborted: open configured-token orders remain after cancellation: "
            + ", ".join(filter(None, (_order_id(row) for row in remaining)))
        )

    async def _cancel_all_tracked_orders(self) -> list[str]:
        failures: list[str] = []
        for order in list(self.orders.values()):
            if await self._cancel_order_reliably(order):
                self.orders.pop(order.order_id, None)
                self._persist_orders()
            else:
                failures.append(order.order_id)
        return failures

    async def _cancel_token_orders(self, token_id: str) -> None:
        for order in list(self.orders.values()):
            if order.quote.token_id != token_id:
                continue
            if await self._cancel_order_reliably(order):
                self.orders.pop(order.order_id, None)
                self._persist_orders()

    async def _handle_fill(self, order: ManagedOrder, source: str) -> None:
        if not self.config.halt_on_fill:
            return
        first_halt = order.quote.token_id not in self.halted_tokens
        self.halted_tokens.add(order.quote.token_id)
        if first_halt:
            logger.critical(
                "%s fill detected for %s (matched=%s); token halted",
                source,
                order.quote.token_id,
                order.filled_size,
            )
        await self._cancel_token_orders(order.quote.token_id)

    async def _watch_user_events(self) -> None:
        condition_ids = list(
            dict.fromkeys(
                market.condition_id
                for market in self.config.enabled_markets
                if market.condition_id
            )
        )
        backoff = 1.0
        while not self.stop.is_set():
            try:
                async for event in self.client.stream_user_events(condition_ids):
                    await self._user_events.put(event)
                    backoff = 1.0
                    if self.stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("User WebSocket disconnected: %s; reconnecting", error)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 30)

    async def _drain_user_events(self) -> None:
        while True:
            try:
                event = self._user_events.get_nowait()
            except asyncio.QueueEmpty:
                return
            await self._handle_user_event(event)

    async def _handle_user_event(self, event: dict) -> None:
        event_kind = str(event.get("event_type") or "").casefold()
        if event_kind == "order":
            order = self.orders.get(str(event.get("id") or ""))
            if order is None:
                return
            filled = Decimal(str(event.get("size_matched") or "0"))
            if filled > order.filled_size:
                order.filled_size = filled
                self._persist_orders()
                await self._handle_fill(order, source="WebSocket")
            if str(event.get("type") or "").upper() == "CANCELLATION":
                self.orders.pop(order.order_id, None)
                self._persist_orders()
        elif event_kind == "trade":
            candidates = list(event.get("maker_orders") or [])
            taker_id = str(event.get("taker_order_id") or "")
            if taker_id:
                candidates.append(
                    {
                        "order_id": taker_id,
                        "matched_amount": event.get("size") or "0",
                        "asset_id": event.get("asset_id"),
                    }
                )
            for item in candidates:
                order = self.orders.get(str(item.get("order_id") or ""))
                if order is None:
                    continue
                filled = Decimal(str(item.get("matched_amount") or "0"))
                if filled > order.filled_size:
                    order.filled_size = filled
                    self._persist_orders()
                await self._handle_fill(order, source="WebSocket")

    async def _wait_for_event_or_poll(self) -> None:
        try:
            event = await asyncio.wait_for(
                self._user_events.get(), timeout=self.config.poll_interval_seconds
            )
        except asyncio.TimeoutError:
            return
        await self._handle_user_event(event)

    async def pause_quotes(self) -> dict:
        """Pause new quotes and cancel every order tracked by this process."""
        self.paused = True
        failures = await self._cancel_all_tracked_orders()
        if failures:
            raise RuntimeError("Unable to confirm cancellation: " + ", ".join(failures))
        logger.warning("Quoting paused from the local console")
        return await self.snapshot()

    async def resume_quotes(self) -> dict:
        if self.stop.is_set():
            raise RuntimeError("The engine is stopping and cannot resume")
        if self.quote_task_expired or (
            self.quote_deadline_at is not None and self.quote_deadline_at <= time()
        ):
            raise RuntimeError("The quote task has expired; set a new validity period first")
        self.paused = False
        logger.warning("Quoting resumed from the local console")
        return await self.snapshot()

    async def set_quote_expiry(self, hours: int, minutes: int) -> dict:
        if isinstance(hours, bool) or isinstance(minutes, bool):
            raise ValueError("Hours and minutes must be integers")
        if not isinstance(hours, int) or not isinstance(minutes, int):
            raise ValueError("Hours and minutes must be integers")
        if not 0 <= hours <= 168 or not 0 <= minutes <= 59:
            raise ValueError("Validity must be between 1 minute and 7 days")
        duration_seconds = (hours * 60 + minutes) * 60
        if not 60 <= duration_seconds <= MAX_QUOTE_DURATION_SECONDS:
            raise ValueError("Validity must be between 1 minute and 7 days")
        if self.stop.is_set():
            raise RuntimeError("The engine is stopping and cannot start a quote task")
        self.quote_deadline_at = time() + duration_seconds
        self.quote_task_expired = False
        self.paused = False
        self._persist_orders()
        logger.warning("Quote-task validity set to %dh %dm; quoting enabled", hours, minutes)
        return await self.snapshot()

    async def clear_quote_expiry(self) -> dict:
        self.quote_deadline_at = None
        self.quote_task_expired = False
        self._persist_orders()
        logger.warning("Quote-task validity cleared; current pause state is unchanged")
        return await self.snapshot()

    async def _expire_quote_task_if_due(self) -> bool:
        if self.quote_deadline_at is None or self.quote_deadline_at > time():
            return False
        first_expiration = not self.quote_task_expired
        self.quote_task_expired = True
        self.paused = True
        self._persist_orders()
        failures = await self._cancel_all_tracked_orders()
        expiration_error_prefix = "Expired quote task could not cancel orders: "
        if failures:
            self.last_error = expiration_error_prefix + ", ".join(failures)
            logger.critical(self.last_error)
        else:
            if self.last_error.startswith(expiration_error_prefix):
                self.last_error = ""
            if first_expiration:
                logger.critical("Quote-task validity expired; all tracked orders canceled")
        return True

    async def emergency_cancel(self) -> dict:
        """Pause and clear all account orders on configured tokens, including manual ones."""
        self.paused = True
        await self._cancel_configured_orders_on_start()
        logger.critical("Emergency configured-token cancellation completed")
        return await self.snapshot()

    async def snapshot(self) -> dict:
        markets = []
        for market in self.config.enabled_markets:
            markets.append(
                {
                    "label": market.label or market.outcome or market.token_id,
                    "token_id": market.token_id,
                    "condition_id": market.condition_id,
                    "halted": market.token_id in self.halted_tokens,
                    "position": str(self.positions.get(market.token_id, Decimal())),
                    "book": self.latest_books.get(market.token_id, {}),
                }
            )
        orders = [
            {
                "order_id": order.order_id,
                "token_id": order.quote.token_id,
                "side": order.quote.side.value,
                "price": str(order.quote.price),
                "size": str(order.quote.size),
                "filled_size": str(order.filled_size),
                "age_seconds": round(order.age_seconds, 1),
            }
            for order in self.orders.values()
        ]
        preflight = None
        if self.preflight_report is not None:
            preflight = {
                "signer_address": self.preflight_report.signer_address,
                "funder_address": self.preflight_report.funder_address,
                "collateral_balance": str(self.preflight_report.collateral_balance),
                "minimum_allowance": str(self.preflight_report.minimum_allowance),
                "country": self.preflight_report.country,
                "region": self.preflight_report.region,
            }
        return {
            "phase": self.phase,
            "dry_run": self.config.dry_run,
            "paused": self.paused,
            "websocket_connected": bool(getattr(self.client, "websocket_connected", False)),
            "started_at": self.started_at,
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "orders": orders,
            "markets": markets,
            "preflight": preflight,
            "quote_task": {
                "deadline_at": self.quote_deadline_at,
                "remaining_seconds": (
                    max(0, int(self.quote_deadline_at - time()))
                    if self.quote_deadline_at is not None
                    else None
                ),
                "expired": self.quote_task_expired,
            },
        }


def _normalise_status(value: object) -> str:
    status = str(value or "").upper()
    for prefix in ("ORDER_STATUS_", "STATUS_"):
        if status.startswith(prefix):
            status = status[len(prefix) :]
    return status


def _order_id(row: dict) -> str:
    return str(row.get("id") or row.get("orderID") or row.get("order_id") or "")


def _matched_shares(state: dict, order: ManagedOrder) -> Decimal:
    matched = Decimal(str(state.get("size_matched") or state.get("sizeMatched") or "0"))
    original_raw = state.get("original_size") or state.get("originalSize")
    if original_raw is None:
        return matched
    original = Decimal(str(original_raw))
    if original <= 0:
        return matched
    # Current order-detail responses describe sizes as 6-decimal fixed math,
    # while some legacy responses expose human shares. A ratio handles both.
    return order.quote.size * matched / original
