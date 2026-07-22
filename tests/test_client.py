from decimal import Decimal
from unittest.mock import Mock, patch

from poly_mm.client import PolymarketClient
from poly_mm.config import Settings
from poly_mm.models import Level, OrderBook, Quote, Side


@patch("poly_mm.client.requests.get")
def test_orderbook_is_sorted_best_price_first(mock_get: Mock) -> None:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "bids": [{"price": "0.01", "size": "10"}, {"price": "0.37", "size": "5"}],
        "asks": [{"price": "0.99", "size": "10"}, {"price": "0.38", "size": "5"}],
        "tick_size": "0.01",
        "min_order_size": "5",
        "neg_risk": False,
    }
    mock_get.return_value = response

    book = PolymarketClient(Settings(), dry_run=True).get_orderbook("yes-id")

    assert str(book.best_bid.price) == "0.37"
    assert str(book.best_ask.price) == "0.38"


def test_sell_limit_is_submitted_non_post_only_at_requested_price() -> None:
    sdk = Mock()
    sdk.create_and_post_order.return_value = {"success": True, "orderID": "sell-1"}
    client = PolymarketClient(Settings(private_key="0x" + "1" * 64), dry_run=False)
    client._sdk = sdk
    client.get_orderbook = Mock(
        return_value=OrderBook(
            token_id="token-1",
            bids=[Level(Decimal("0.35"), Decimal("100"))],
            asks=[Level(Decimal("0.36"), Decimal("100"))],
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
        )
    )
    quote = Quote("token-1", Side.SELL, Decimal("0.01"), Decimal("25"))

    order = client.create_order(quote, post_only=False)

    call = sdk.create_and_post_order.call_args
    assert order.order_id == "sell-1"
    assert call.kwargs["order_args"].side == "SELL"
    assert call.kwargs["order_args"].price == 0.01
    assert call.kwargs["order_args"].size == 25.0
    assert call.kwargs["order_type"] == "GTC"
    assert call.kwargs["post_only"] is False
