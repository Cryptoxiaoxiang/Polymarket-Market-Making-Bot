from unittest.mock import Mock, patch

from poly_mm.client import PolymarketClient
from poly_mm.config import Settings


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
