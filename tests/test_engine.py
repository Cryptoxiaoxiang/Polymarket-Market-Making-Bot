import asyncio
from decimal import Decimal
from types import SimpleNamespace

from poly_mm.config import BotConfig, MarketConfig
from poly_mm.engine import MarketMakerEngine
from poly_mm.journal import OrderJournal
from poly_mm.models import ManagedOrder, Quote, Side


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

    def get_order(self, order_id: str) -> dict:
        return self.order_state

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.open_orders = [row for row in self.open_orders if row["id"] != order_id]

    def get_open_orders(self, token_id: str | None = None) -> list[dict]:
        return list(self.open_orders)


def _order() -> ManagedOrder:
    return ManagedOrder(
        "order-1",
        Quote("token-1", Side.BUY, Decimal("0.42"), Decimal("5")),
        1_700_000_000,
    )


def _config(*, dry_run: bool = False) -> BotConfig:
    return BotConfig(
        dry_run=dry_run,
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


def test_rest_error_removes_order_only_when_open_orders_confirms_absence(tmp_path) -> None:
    client = FakeClient(tmp_path / "orders.json")
    client.get_order = lambda order_id: (_ for _ in ()).throw(RuntimeError("not found"))
    engine = MarketMakerEngine(_config(), client)
    engine.orders = {"order-1": _order()}

    asyncio.run(engine._reconcile_orders())

    assert engine.orders == {}


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
