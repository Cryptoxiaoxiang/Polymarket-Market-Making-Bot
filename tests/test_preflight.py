from decimal import Decimal
from unittest.mock import Mock

import pytest

from poly_mm.client import PolymarketClient
from poly_mm.config import BotConfig, MarketConfig, RiskConfig, Settings
from poly_mm.models import Level, OrderBook


def _live_config(required: str = "100") -> BotConfig:
    return BotConfig(
        dry_run=False,
        risk=RiskConfig(max_order_size=Decimal(required)),
        markets=[MarketConfig(token_id="token-1")],
    )


def test_eoa_preflight_checks_balance_allowances_and_l2() -> None:
    sdk = Mock()
    sdk.get_balance_allowance.return_value = {
        "balance": "250000000",
        "allowances": {"exchange": "150000000", "neg-risk": "120000000"},
    }
    sdk.get_open_orders.return_value = []
    client = PolymarketClient(
        Settings(private_key="not-used", funder="0xabc", signature_type=0),
        dry_run=False,
    )
    client.signer_address = Mock(return_value="0xAbC")
    client.check_geoblock = Mock(
        return_value={"blocked": False, "country": "CA", "region": "BC"}
    )
    client.planned_buy_collateral = Mock(
        return_value=(Decimal("49"), Decimal("100"))
    )
    client._authenticated_sdk = Mock(return_value=sdk)

    report = client.run_preflight(_live_config())

    assert report.collateral_balance == Decimal("250")
    assert report.minimum_allowance == Decimal("120")
    sdk.update_balance_allowance.assert_called_once()
    sdk.get_open_orders.assert_called_once_with(only_first_page=True)


def test_eoa_preflight_rejects_different_funder() -> None:
    client = PolymarketClient(
        Settings(private_key="not-used", funder="0xdef", signature_type=0),
        dry_run=False,
    )
    client.signer_address = Mock(return_value="0xabc")

    with pytest.raises(RuntimeError, match="requires.*match"):
        client.run_preflight(_live_config())


def test_preflight_rejects_blocked_vps_before_authentication() -> None:
    client = PolymarketClient(
        Settings(private_key="not-used", funder="0xabc", signature_type=0),
        dry_run=False,
    )
    client.signer_address = Mock(return_value="0xabc")
    client.check_geoblock = Mock(
        return_value={"blocked": True, "country": "US", "region": "NY"}
    )
    client._authenticated_sdk = Mock()

    with pytest.raises(RuntimeError, match="blocked.*US/NY"):
        client.run_preflight(_live_config())
    client._authenticated_sdk.assert_not_called()


def test_preflight_allows_japan_frontend_only_restriction() -> None:
    sdk = Mock()
    sdk.get_balance_allowance.return_value = {
        "balance": "250000000",
        "allowances": {"exchange": "150000000"},
    }
    sdk.get_open_orders.return_value = []
    client = PolymarketClient(
        Settings(private_key="not-used", funder="0xabc", signature_type=0),
        dry_run=False,
    )
    client.signer_address = Mock(return_value="0xabc")
    client.check_geoblock = Mock(
        return_value={"blocked": True, "country": "JP", "region": "13"}
    )
    client.planned_buy_collateral = Mock(
        return_value=(Decimal("49"), Decimal("100"))
    )
    client._authenticated_sdk = Mock(return_value=sdk)

    report = client.run_preflight(_live_config())

    assert report.country == "JP"
    assert report.region == "13"
    sdk.get_open_orders.assert_called_once_with(only_first_page=True)


def test_preflight_rejects_insufficient_allowance() -> None:
    sdk = Mock()
    sdk.get_balance_allowance.return_value = {
        "balance": "250000000",
        "allowances": {"exchange": "99000000"},
    }
    client = PolymarketClient(
        Settings(private_key="not-used", funder="0xabc", signature_type=0),
        dry_run=False,
    )
    client.signer_address = Mock(return_value="0xabc")
    client.check_geoblock = Mock(return_value={"blocked": False})
    client.planned_buy_collateral = Mock(
        return_value=(Decimal("100"), Decimal("100"))
    )
    client._authenticated_sdk = Mock(return_value=sdk)

    with pytest.raises(RuntimeError, match="Insufficient CLOB allowance"):
        client.run_preflight(_live_config())


def test_preflight_prices_share_quantity_as_current_quote_notional() -> None:
    client = PolymarketClient(Settings(), dry_run=False)
    client.get_orderbook = Mock(
        return_value=OrderBook(
            "token-1",
            bids=[Level(Decimal("0.50"), Decimal("200"))],
            asks=[Level(Decimal("0.54"), Decimal("200"))],
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
        )
    )
    config = BotConfig(
        dry_run=False,
        markets=[MarketConfig(token_id="token-1", quote_size=Decimal("100"))],
    )

    required, shares = client.planned_buy_collateral(config)

    assert shares == Decimal("100")
    assert required == Decimal("49")
