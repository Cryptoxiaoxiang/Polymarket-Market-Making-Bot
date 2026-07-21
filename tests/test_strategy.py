from decimal import Decimal

from poly_mm.config import MarketConfig, StrategyConfig
from poly_mm.models import Level, OrderBook
from poly_mm.strategy import PassiveMakerStrategy


def test_quotes_one_tick_behind_best_bid() -> None:
    book = OrderBook("yes", [Level(Decimal(".50"), Decimal("10"))], [Level(Decimal(".54"), Decimal("10"))], Decimal(".01"), Decimal("5"))
    quote = PassiveMakerStrategy(StrategyConfig(quote_size=Decimal("5"))).build_quote(MarketConfig("yes"), book)
    assert quote is not None
    assert quote.price == Decimal(".49")


def test_does_not_quote_tight_spread() -> None:
    book = OrderBook("yes", [Level(Decimal(".50"), Decimal("10"))], [Level(Decimal(".51"), Decimal("10"))], Decimal(".01"), Decimal("5"))
    assert PassiveMakerStrategy(StrategyConfig()).build_quote(MarketConfig("yes"), book) is None


def test_risk_counts_existing_positions() -> None:
    from poly_mm.config import RiskConfig
    from poly_mm.models import Quote, Side
    from poly_mm.risk import RiskManager

    quote = Quote("yes", Side.BUY, Decimal(".49"), Decimal("5"))
    risk = RiskManager(RiskConfig(max_position_per_token=Decimal("5")))

    assert not risk.approve(quote, [], {"yes": Decimal("1")})
