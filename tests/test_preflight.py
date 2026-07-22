from decimal import Decimal
from unittest.mock import Mock

import pytest

from poly_mm.client import PolymarketClient
from poly_mm.config import BotConfig, MarketConfig, Settings


def _live_config() -> BotConfig:
    return BotConfig(
        dry_run=False,
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
    client._authenticated_sdk = Mock(return_value=sdk)

    report = client.run_preflight(_live_config())

    assert report.country == "JP"
    assert report.region == "13"
    sdk.get_open_orders.assert_called_once_with(only_first_page=True)


def test_preflight_does_not_aggregate_collateral_across_markets() -> None:
    sdk = Mock()
    sdk.get_balance_allowance.return_value = {
        "balance": "54454155",
        "allowances": {"exchange": "50000000"},
    }
    sdk.get_open_orders.return_value = []
    client = PolymarketClient(
        Settings(private_key="not-used", funder="0xabc", signature_type=0),
        dry_run=False,
    )
    client.signer_address = Mock(return_value="0xabc")
    client.check_geoblock = Mock(return_value={"blocked": False})
    client.get_orderbook = Mock(
        side_effect=AssertionError("preflight must not price or aggregate planned orders")
    )
    client._authenticated_sdk = Mock(return_value=sdk)
    seven_markets = BotConfig(
        dry_run=False,
        markets=[MarketConfig(token_id=f"token-{index}") for index in range(7)],
    )

    report = client.run_preflight(seven_markets)

    assert report.collateral_balance == Decimal("54.454155")
    assert report.minimum_allowance == Decimal("50")
    client.get_orderbook.assert_not_called()
    sdk.get_open_orders.assert_called_once_with(only_first_page=True)
