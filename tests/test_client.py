from decimal import Decimal
from unittest.mock import Mock, patch

from poly_mm.client import PolymarketClient
from poly_mm.config import Settings
from poly_mm.models import Level, ManagedOrder, OrderBook, Quote, Side


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


def test_cached_tick_size_skips_orderbook_request() -> None:
    sdk = Mock()
    sdk.create_and_post_order.return_value = {"success": True, "orderID": "sell-1"}
    client = PolymarketClient(Settings(private_key="0x" + "1" * 64), dry_run=False)
    client._sdk = sdk
    client.get_orderbook = Mock(side_effect=AssertionError("unexpected orderbook request"))
    quote = Quote("token-1", Side.SELL, Decimal("0.01"), Decimal("25"))

    order = client.create_order(
        quote, post_only=False, tick_size=Decimal("0.01")
    )

    assert order.order_id == "sell-1"
    options = sdk.create_and_post_order.call_args.kwargs["options"]
    assert options.tick_size == "0.01"
    client.get_orderbook.assert_not_called()


def test_batch_orders_are_signed_and_posted_in_one_request() -> None:
    sdk = Mock()
    sdk.create_order.side_effect = ["signed-1", "signed-2"]
    sdk.post_orders.return_value = [
        {"success": True, "orderID": "order-1"},
        {
            "success": False,
            "errorMsg": "not enough balance / allowance",
        },
    ]
    client = PolymarketClient(Settings(private_key="0x" + "1" * 64), dry_run=False)
    client._sdk = sdk
    quotes = [
        (
            Quote("token-1", Side.BUY, Decimal("0.20"), Decimal("100")),
            Decimal("0.01"),
        ),
        (
            Quote("token-2", Side.BUY, Decimal("0.30"), Decimal("100")),
            Decimal("0.001"),
        ),
    ]

    results = client.create_orders_batch(quotes)

    assert sdk.create_order.call_count == 2
    assert sdk.post_orders.call_count == 1
    post_args = sdk.post_orders.call_args
    assert [item.order for item in post_args.args[0]] == ["signed-1", "signed-2"]
    assert post_args.kwargs["post_only"] is True
    assert isinstance(results[0], ManagedOrder)
    assert results[0].order_id == "order-1"
    assert results[0].quote == quotes[0][0]
    assert isinstance(results[1], RuntimeError)
    assert "not enough balance / allowance" in str(results[1])


def test_submission_retry_reuses_the_same_signed_order() -> None:
    sdk = Mock()
    prepared = object()
    sdk.create_order.return_value = prepared
    sdk.post_order.side_effect = [
        RuntimeError("Request exception"),
        {"success": True, "orderID": "sell-1"},
    ]
    client = PolymarketClient(Settings(private_key="0x" + "1" * 64), dry_run=False)
    client._sdk = sdk
    quote = Quote("token-1", Side.SELL, Decimal("0.01"), Decimal("1.18"))

    try:
        client.create_order(
            quote,
            post_only=False,
            tick_size=Decimal("0.01"),
            submission_key="fill-1",
        )
    except RuntimeError as error:
        assert str(error) == "Request exception"
    else:
        raise AssertionError("first post should simulate a lost HTTP response")

    order = client.create_order(
        quote,
        post_only=False,
        tick_size=Decimal("0.01"),
        submission_key="fill-1",
    )

    assert order.order_id == "sell-1"
    sdk.create_order.assert_called_once()
    assert sdk.post_order.call_count == 2
    assert all(call.args[0] is prepared for call in sdk.post_order.call_args_list)
    assert client._prepared_orders == {}


@patch("py_clob_client_v2.ClobClient")
def test_authenticated_client_uses_ntp_synced_local_time(mock_client: Mock) -> None:
    credential_client = Mock()
    credential_client.create_or_derive_api_key.return_value = Mock()
    authenticated_client = Mock()
    mock_client.side_effect = [credential_client, authenticated_client]
    client = PolymarketClient(
        Settings(private_key="0x" + "1" * 64),
        dry_run=False,
    )

    assert client._authenticated_sdk() is authenticated_client
    assert mock_client.call_count == 2
    assert all(
        call.kwargs["use_server_time"] is False for call in mock_client.call_args_list
    )


def test_order_matched_shares_is_recovered_from_taker_and_maker_trades() -> None:
    sdk = Mock()
    sdk.get_trades.return_value = [
        {
            "id": "trade-1",
            "status": "TRADE_STATUS_CONFIRMED",
            "taker_order_id": "order-1",
            "size": "1.5",
            "maker_orders": [],
        },
        {
            "id": "trade-2",
            "status": "MATCHED",
            "maker_orders": [
                {"order_id": "order-1", "matched_amount": "2.5"},
            ],
        },
        {
            "id": "trade-2",
            "status": "CONFIRMED",
            "maker_orders": [
                {"order_id": "order-1", "matched_amount": "2.5"},
            ],
        },
        {
            "id": "trade-3",
            "status": "TRADE_STATUS_FAILED",
            "taker_order_id": "order-1",
            "size": "10",
        },
    ]
    client = PolymarketClient(Settings(private_key="0x" + "1" * 64), dry_run=False)
    client._sdk = sdk

    matched = client.get_order_matched_shares("order-1", "token-1", 1_700_000_000)

    assert matched == Decimal("4.0")
    params = sdk.get_trades.call_args.args[0]
    assert params.asset_id == "token-1"
    assert params.after == 1_699_999_940
    assert sdk.get_trades.call_args.kwargs["only_first_page"] is True
