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


def test_spread_thresholds_scale_with_fine_tick_size() -> None:
    book = OrderBook(
        "yes",
        [Level(Decimal(".252"), Decimal("100"))],
        [Level(Decimal(".254"), Decimal("100"))],
        Decimal(".001"),
        Decimal("5"),
    )

    quote = PassiveMakerStrategy(StrategyConfig()).build_quote(
        MarketConfig("yes"), book
    )

    assert quote is not None
    assert quote.price == Decimal(".251")


def test_fine_tick_spread_still_respects_scaled_minimum_and_maximum() -> None:
    strategy = PassiveMakerStrategy(StrategyConfig())
    below_minimum = OrderBook(
        "yes",
        [Level(Decimal(".252"), Decimal("100"))],
        [Level(Decimal(".253"), Decimal("100"))],
        Decimal(".001"),
        Decimal("5"),
    )
    above_maximum = OrderBook(
        "yes",
        [Level(Decimal(".252"), Decimal("100"))],
        [Level(Decimal(".268"), Decimal("100"))],
        Decimal(".001"),
        Decimal("5"),
    )

    assert strategy.build_quote(MarketConfig("yes"), below_minimum) is None
    assert strategy.build_quote(MarketConfig("yes"), above_maximum) is None


def test_risk_counts_existing_positions() -> None:
    from poly_mm.config import RiskConfig
    from poly_mm.models import Quote, Side
    from poly_mm.risk import RiskManager

    quote = Quote("yes", Side.BUY, Decimal(".49"), Decimal("5"))
    risk = RiskManager(RiskConfig(max_position_per_token=Decimal("5")))

    assert not risk.approve(quote, [], {"yes": Decimal("1")})


def test_risk_caps_total_remaining_open_order_shares_not_notional() -> None:
    from poly_mm.config import RiskConfig
    from poly_mm.models import ManagedOrder, Quote, Side
    from poly_mm.risk import RiskManager

    existing = ManagedOrder(
        "order-1",
        Quote("no", Side.BUY, Decimal(".01"), Decimal("5")),
        1_700_000_000,
        filled_size=Decimal("2"),
    )
    risk = RiskManager(RiskConfig(max_total_open_shares=Decimal("6")))

    assert risk.approve(
        Quote("yes", Side.BUY, Decimal(".99"), Decimal("3")), [existing]
    )
    assert not risk.approve(
        Quote("yes", Side.BUY, Decimal(".01"), Decimal("3.01")), [existing]
    )
