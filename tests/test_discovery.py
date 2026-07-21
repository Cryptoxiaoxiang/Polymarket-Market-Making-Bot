from unittest.mock import Mock, patch

import pytest

from poly_mm.config import MarketConfig
from poly_mm.discovery import resolve_market


def _response(data: dict) -> Mock:
    response = Mock()
    response.json.return_value = data
    response.raise_for_status.return_value = None
    return response


@patch("poly_mm.discovery.requests.get")
def test_resolves_event_url_outcome(mock_get: Mock) -> None:
    mock_get.return_value = _response({
        "markets": [{
            "question": "Will it happen?", "slug": "will-it-happen",
            "conditionId": "0xabc", "active": True, "closed": False,
            "enableOrderBook": True, "acceptingOrders": True,
            "outcomes": '["Yes", "No"]', "clobTokenIds": '["yes-id", "no-id"]',
        }]
    })
    resolved = resolve_market(MarketConfig(
        url="https://polymarket.com/event/will-it-happen", outcome="No"
    ))
    assert resolved.token_id == "no-id"
    assert resolved.condition_id == "0xabc"
    assert resolved.label == "Will it happen? — No"


def test_rejects_non_polymarket_url() -> None:
    with pytest.raises(ValueError, match="polymarket.com"):
        resolve_market(MarketConfig(url="https://example.com/event/test"))
