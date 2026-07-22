from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from time import time


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: list[Level]
    asks: list[Level]
    tick_size: Decimal
    min_order_size: Decimal
    neg_risk: bool = False

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if not self.best_bid or not self.best_ask:
            return None
        return self.best_ask.price - self.best_bid.price


@dataclass(frozen=True)
class Quote:
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    neg_risk: bool = False


@dataclass
class ManagedOrder:
    order_id: str
    quote: Quote
    created_at: float
    filled_size: Decimal = Decimal("0")
    exit_requested_size: Decimal = Decimal("0")

    @property
    def age_seconds(self) -> float:
        return max(0.0, time() - self.created_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "created_at": self.created_at,
            "filled_size": str(self.filled_size),
            "exit_requested_size": str(self.exit_requested_size),
            "quote": {
                "token_id": self.quote.token_id,
                "side": self.quote.side.value,
                "price": str(self.quote.price),
                "size": str(self.quote.size),
                "neg_risk": self.quote.neg_risk,
            },
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ManagedOrder":
        quote_raw = raw["quote"]
        quote = Quote(
            token_id=str(quote_raw["token_id"]),
            side=Side(str(quote_raw["side"])),
            price=Decimal(str(quote_raw["price"])),
            size=Decimal(str(quote_raw["size"])),
            neg_risk=bool(quote_raw.get("neg_risk", False)),
        )
        return cls(
            order_id=str(raw["order_id"]),
            quote=quote,
            created_at=float(raw["created_at"]),
            filled_size=Decimal(str(raw.get("filled_size", "0"))),
            exit_requested_size=Decimal(str(raw.get("exit_requested_size", "0"))),
        )


@dataclass(frozen=True)
class ExitIntent:
    """Crash-safe instruction to submit a protective sell for a detected buy fill."""

    intent_id: str
    source_order_id: str
    token_id: str
    size: Decimal
    neg_risk: bool
    created_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "source_order_id": self.source_order_id,
            "token_id": self.token_id,
            "size": str(self.size),
            "neg_risk": self.neg_risk,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ExitIntent":
        return cls(
            intent_id=str(raw["intent_id"]),
            source_order_id=str(raw["source_order_id"]),
            token_id=str(raw["token_id"]),
            size=Decimal(str(raw["size"])),
            neg_risk=bool(raw.get("neg_risk", False)),
            created_at=float(raw["created_at"]),
        )


@dataclass(frozen=True)
class PreflightReport:
    signer_address: str
    funder_address: str
    collateral_balance: Decimal
    minimum_allowance: Decimal
    country: str = ""
    region: str = ""


def round_down_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    return (value / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size
