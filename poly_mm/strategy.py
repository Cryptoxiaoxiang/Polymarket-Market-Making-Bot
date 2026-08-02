from __future__ import annotations

from decimal import Decimal

from poly_mm.config import MarketConfig, StrategyConfig
from poly_mm.models import OrderBook, Quote, Side, round_down_to_tick

STANDARD_SPREAD_TICK = Decimal("0.01")


class PassiveMakerStrategy:
    """Long-only inventory model: bid a single outcome, never crosses the spread."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def build_quote(self, market: MarketConfig, book: OrderBook) -> Quote | None:
        if not book.best_bid or not book.best_ask or book.spread is None:
            return None
        spread_scale = min(Decimal(1), book.tick_size / STANDARD_SPREAD_TICK)
        minimum_spread = self.config.min_spread * spread_scale
        maximum_spread = self.config.max_spread * spread_scale
        if not minimum_spread <= book.spread <= maximum_spread:
            return None
        raw_price = book.best_bid.price if self.config.join_best_price else (
            book.best_bid.price - book.tick_size * self.config.min_edge_ticks
        )
        price = round_down_to_tick(raw_price, book.tick_size)
        size = market.quote_size or self.config.quote_size
        if price <= 0 or price >= book.best_ask.price or size < book.min_order_size:
            return None
        return Quote(
            token_id=market.token_id,
            side=Side.BUY,
            price=price,
            size=size,
            neg_risk=book.neg_risk,
        )
