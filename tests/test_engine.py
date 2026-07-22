import asyncio
from decimal import Decimal
from time import time
from types import SimpleNamespace

from poly_mm.config import BotConfig, MarketConfig
from poly_mm.engine import MarketMakerEngine
from poly_mm.journal import OrderJournal
from poly_mm.models import ExitIntent, ManagedOrder, Quote, Side


class FakeClient:
    def __init__(self, journal_path, order_state: dict | None = None) -> None:
        self.settings = SimpleNamespace(order_journal_path=str(journal_path))
        self.order_state = order_state or {
            "id": "order-1",
            "status": "ORDER_STATUS_LIVE",
            "size_matched": "0",
        }
        self.open_orders: list[dict] = []
        self.cancelled: list[str] = []
        self.created: list[tuple[Quote, bool]] = []
        self.synced_tokens: list[str] = []
        self.matched_shares = Decimal()
        self.positions: dict[str, Decimal] = {}

    def get_order(self, order_id: str) -> dict:
        return self.order_state

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.open_orders = [row for row in self.open_orders if row["id"] != order_id]

    def cancel_market_orders(self, condition_id: str, token_id: str) -> dict:
        self.open_orders = []
        return {}

    def get_open_orders(self, token_id: str | None = None) -> list[dict]:
        return list(self.open_orders)

    def get_order_matched_shares(
        self, order_id: str, token_id: str, created_at: float | None = None
    ) -> Decimal:
        return self.matched_shares

    def get_positions(self, condition_ids: list[str] | None = None) -> dict[str, Decimal]:
        return dict(self.positions)

    def sync_conditional_allowance(self, token_id: str) -> None:
        self.synced_tokens.append(token_id)

    def create_order(self, quote: Quote, *, post_only: bool = True) -> ManagedOrder:
        self.created.append((quote, post_only))
        return ManagedOrder("exit-order", quote, time())


def _order() -> ManagedOrder:
    return ManagedOrder(
        "order-1",
        Quote("token-1", Side.BUY, Decimal("0.42"), Decimal("5")),
        1_700_000_000,
    )


def _config(*, dry_run: bool = False) -> BotConfig:
    return BotConfig(
        dry_run=dry_run,
        sell_on_fill=False,
        cancel_retry_base_seconds=0,
        markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
    )


def test_dry_run_does_not_read_or_overwrite_live_journal(tmp_path) -> None:
    journal = OrderJournal(tmp_path / "orders.json")
    journal.save([_order()])
    client = FakeClient(journal.path)
    engine = MarketMakerEngine(_config(dry_run=True), client, journal)

    engine._restore_orders()
    engine._persist_orders()

    assert engine.orders == {}
    assert journal.load() == [_order()]


def test_unknown_rest_status_retains_order_for_safety(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json", {"status": "MYSTERY"})
    engine = MarketMakerEngine(_config(), client)
    engine.orders = {"order-1": _order()}

    asyncio.run(engine._reconcile_orders())

    assert "order-1" in engine.orders


def test_rest_error_requires_repeated_trade_confirmation_before_removal(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    client.get_order = lambda order_id: (_ for _ in ()).throw(RuntimeError("not found"))
    engine = MarketMakerEngine(_config(), client)
    engine.orders = {"order-1": _order()}

    asyncio.run(engine._reconcile_orders())
    asyncio.run(engine._reconcile_orders())

    assert "order-1" in engine.orders

    asyncio.run(engine._reconcile_orders())

    assert engine.orders == {}


def test_empty_rest_state_requires_repeated_trade_confirmation_before_removal(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    client.order_state = None
    engine = MarketMakerEngine(_config(), client)
    engine.orders = {"order-1": _order()}

    asyncio.run(engine._reconcile_orders())
    asyncio.run(engine._reconcile_orders())

    assert "order-1" in engine.orders

    asyncio.run(engine._reconcile_orders())

    assert engine.orders == {}


def test_missing_order_uses_trade_history_to_submit_fill_exit(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    client.order_state = None
    client.matched_shares = Decimal("2")
    engine = MarketMakerEngine(
        BotConfig(
            sell_on_fill=True,
            cancel_retry_base_seconds=0,
            markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
        ),
        client,
    )
    engine.orders = {"order-1": _order()}

    async def scenario() -> None:
        await engine._reconcile_orders()
        await asyncio.gather(*engine._exit_tasks)

    asyncio.run(scenario())

    quote, post_only = client.created[0]
    assert quote.side == Side.SELL
    assert quote.price == Decimal("0.01")
    assert quote.size == Decimal("2")
    assert post_only is False
    assert engine.pending_exits == {}


def test_restored_unprotected_fill_uses_trade_history_to_submit_exit(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    client.order_state = None
    client.matched_shares = Decimal("2")
    engine = MarketMakerEngine(
        BotConfig(
            sell_on_fill=True,
            cancel_retry_base_seconds=0,
            markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
        ),
        client,
    )
    order = _order()
    order.filled_size = Decimal("2")
    engine.orders = {order.order_id: order}

    async def scenario() -> None:
        await engine._reconcile_orders()
        await asyncio.gather(*engine._exit_tasks)

    asyncio.run(scenario())

    assert client.created[0][0].size == Decimal("2")
    assert order.exit_requested_size == Decimal("2")


def test_rest_fixed_math_fill_is_converted_to_shares(tmp_path) -> None:
    client = FakeClient(
        tmp_path / "orders.json",
        {
            "status": "ORDER_STATUS_LIVE",
            "original_size": "5000000",
            "size_matched": "1250000",
        },
    )
    client.open_orders = [{"id": "order-1"}]
    engine = MarketMakerEngine(_config(), client)
    engine.orders = {"order-1": _order()}

    asyncio.run(engine._reconcile_orders())

    assert engine.halted_tokens == {"token-1"}
    assert client.cancelled == ["order-1"]


def test_websocket_partial_fill_halts_token_and_cancels_order(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    client.open_orders = [{"id": "order-1"}]
    engine = MarketMakerEngine(_config(), client)
    engine.orders = {"order-1": _order()}

    asyncio.run(
        engine._handle_user_event(
            {
                "event_type": "order",
                "id": "order-1",
                "type": "UPDATE",
                "size_matched": "1.25",
            }
        )
    )

    assert "token-1" in engine.halted_tokens
    assert client.cancelled == ["order-1"]
    assert engine.orders == {}
    assert OrderJournal(tmp_path / "orders.json").load() == []


def test_buy_fill_submits_same_size_sell_at_one_cent(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    client.open_orders = [{"id": "order-1"}]
    engine = MarketMakerEngine(
        BotConfig(
            sell_on_fill=True,
            cancel_retry_base_seconds=0,
            markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
        ),
        client,
    )
    order = _order()
    order.filled_size = Decimal("1.25")
    engine.orders = {order.order_id: order}

    async def scenario() -> None:
        await engine._handle_fill(order, source="WebSocket")
        await asyncio.gather(*engine._exit_tasks)

    asyncio.run(scenario())

    assert client.cancelled == ["order-1"]
    assert client.synced_tokens == ["token-1"]
    quote, post_only = client.created[0]
    assert quote.side == Side.SELL
    assert quote.price == Decimal("0.01")
    assert quote.size == Decimal("1.25")
    assert post_only is False
    assert engine.pending_exits == {}
    assert engine.orders["exit-order"].quote.side == Side.SELL


def test_fill_exit_retries_until_shares_are_available(tmp_path) -> None:
    class DelayedSharesClient(FakeClient):
        def __init__(self, journal_path) -> None:
            super().__init__(journal_path)
            self.attempts = 0

        def create_order(self, quote: Quote, *, post_only: bool = True) -> ManagedOrder:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("not enough balance / allowance")
            return super().create_order(quote, post_only=post_only)

    client = DelayedSharesClient(tmp_path / "orders.json")
    client.open_orders = [{"id": "order-1"}]
    engine = MarketMakerEngine(
        BotConfig(
            sell_on_fill=True,
            cancel_retry_base_seconds=0,
            markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
        ),
        client,
    )
    engine._exit_retry_base_seconds = 0
    order = _order()
    order.filled_size = Decimal("2")
    engine.orders = {order.order_id: order}

    async def scenario() -> None:
        await engine._handle_fill(order, source="REST")
        await asyncio.gather(*engine._exit_tasks)

    asyncio.run(scenario())

    assert client.attempts == 2
    assert client.created[0][0].size == Decimal("2")
    assert engine.pending_exits == {}


def test_position_increase_for_tracked_buy_submits_fill_exit(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    engine = MarketMakerEngine(
        BotConfig(
            sell_on_fill=True,
            cancel_retry_base_seconds=0,
            markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
        ),
        client,
    )

    async def scenario() -> None:
        await engine._refresh_positions_if_due()
        order = _order()
        engine.orders = {order.order_id: order}
        client.open_orders = [{"id": order.order_id}]
        client.positions = {"token-1": Decimal("5")}
        engine._next_position_poll_at = 0
        await engine._refresh_positions_if_due()
        await asyncio.gather(*engine._exit_tasks)

    asyncio.run(scenario())

    quote, post_only = client.created[0]
    assert quote.side == Side.SELL
    assert quote.price == Decimal("0.01")
    assert quote.size == Decimal("5")
    assert post_only is False
    assert "token-1" in engine.halted_tokens


def test_position_existing_before_start_is_halted_but_not_sold(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    client.positions = {"token-1": Decimal("5")}
    engine = MarketMakerEngine(
        BotConfig(
            sell_on_fill=True,
            cancel_retry_base_seconds=0,
            markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
        ),
        client,
    )

    asyncio.run(engine._refresh_positions_if_due())

    assert client.created == []
    assert "token-1" in engine.halted_tokens


def test_startup_cancellation_keeps_restored_orders_for_fill_reconciliation(
    tmp_path,
) -> None:
    client = FakeClient(tmp_path / "orders.json")
    engine = MarketMakerEngine(_config(), client)
    engine.orders = {"order-1": _order()}

    asyncio.run(engine._cancel_configured_orders_on_start())

    assert "order-1" in engine.orders


def test_post_cancel_reconciliation_exits_late_partial_fill(tmp_path) -> None:
    client = FakeClient(
        tmp_path / "orders.json",
        {"status": "ORDER_STATUS_MATCHED", "size_matched": "2.5"},
    )
    client.open_orders = [{"id": "order-1"}]
    engine = MarketMakerEngine(
        BotConfig(
            sell_on_fill=True,
            cancel_retry_base_seconds=0,
            markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
        ),
        client,
    )
    order = _order()
    order.filled_size = Decimal("1")
    engine.orders = {order.order_id: order}

    async def scenario() -> None:
        await engine._handle_fill(order, source="WebSocket")
        await asyncio.gather(*engine._exit_tasks)

    asyncio.run(scenario())

    assert sorted(quote.size for quote, _ in client.created) == [
        Decimal("1"),
        Decimal("1.5"),
    ]
    assert order.exit_requested_size == Decimal("2.5")


def test_pending_fill_exit_survives_restart(tmp_path) -> None:
    journal = OrderJournal(tmp_path / "orders.json")
    intent = ExitIntent(
        intent_id="buy-1:3",
        source_order_id="buy-1",
        token_id="token-1",
        size=Decimal("3"),
        neg_risk=False,
        created_at=time(),
    )
    journal.save([], pending_exits=[intent])
    client = FakeClient(journal.path)
    engine = MarketMakerEngine(_config(), client, journal)
    engine._restore_orders()

    async def scenario() -> None:
        engine._resume_pending_exits()
        await asyncio.gather(*engine._exit_tasks)

    asyncio.run(scenario())

    assert client.created[0][0].side == Side.SELL
    assert client.created[0][0].price == Decimal("0.01")
    assert client.created[0][0].size == Decimal("3")
    assert journal.load_pending_exits() == []


def test_expired_quote_task_pauses_and_cancels(tmp_path) -> None:
    journal = OrderJournal(tmp_path / "orders.json")
    client = FakeClient(journal.path)
    client.open_orders = [{"id": "order-1"}]
    engine = MarketMakerEngine(_config(), client, journal)
    engine.orders = {"order-1": _order()}
    engine.quote_deadline_at = time() - 1

    expired = asyncio.run(engine._expire_quote_task_if_due())

    assert expired is True
    assert engine.paused is True
    assert engine.quote_task_expired is True
    assert client.cancelled == ["order-1"]
    assert journal.load() == []



def test_each_task_start_recalculates_validity_and_clears_old_pause(tmp_path) -> None:
    journal = OrderJournal(tmp_path / "orders.json")
    journal.save([], quote_deadline_at=time() - 60)
    config = BotConfig(
        dry_run=True,
        run_duration_seconds=5_400,
        markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
    )
    engine = MarketMakerEngine(config, FakeClient(journal.path), journal)
    engine.paused = True
    engine.quote_task_expired = True

    engine._restore_orders()
    engine._reset_quote_task_for_start()

    assert engine.paused is False
    assert engine.quote_task_expired is False
    assert engine.quote_deadline_at is not None
    assert 5_390 <= engine.quote_deadline_at - time() <= 5_400


def test_setting_new_validity_resumes_an_expired_task(tmp_path) -> None:
    engine = MarketMakerEngine(_config(), FakeClient(tmp_path / "orders.json"))
    engine.paused = True
    engine.quote_task_expired = True

    snapshot = asyncio.run(engine.set_quote_expiry(1, 30))

    assert engine.paused is False
    assert engine.quote_task_expired is False
    assert 5_390 <= snapshot["quote_task"]["remaining_seconds"] <= 5_400
