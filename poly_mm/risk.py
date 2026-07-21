from __future__ import annotations

from decimal import Decimal

from poly_mm.config import RiskConfig
from poly_mm.models import ManagedOrder, Quote


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def approve(
        self,
        quote: Quote,
        orders: list[ManagedOrder],
        positions: dict[str, Decimal] | None = None,
    ) -> bool:
        positions = positions or {}
        if quote.size > self.config.max_order_size:
            return False
        token_orders = [order for order in orders if order.quote.token_id == quote.token_id]
        if len(token_orders) >= self.config.max_open_orders_per_token:
            return False
        token_shares = positions.get(quote.token_id, Decimal()) + sum(
            (order.quote.size - order.filled_size for order in token_orders), Decimal()
        )
        if token_shares + quote.size > self.config.max_position_per_token:
            return False
        open_notional = sum((order.quote.price * order.quote.size for order in orders), Decimal())
        return open_notional + quote.price * quote.size <= self.config.max_total_open_notional
