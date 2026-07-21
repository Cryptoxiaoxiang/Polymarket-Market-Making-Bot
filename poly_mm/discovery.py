from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import urlparse

import requests

from poly_mm.config import MarketConfig

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


def resolve_market(market: MarketConfig) -> MarketConfig:
    """Resolve a Polymarket page URL to the selected outcome token via Gamma."""
    if market.token_id:
        return market

    parsed = urlparse(market.url)
    if parsed.scheme != "https" or parsed.hostname not in {"polymarket.com", "www.polymarket.com"}:
        raise ValueError("Market URL must be an https://polymarket.com URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] not in {"event", "market"}:
        raise ValueError("Expected a Polymarket /event/<slug> or /market/<slug> URL")

    page_type, page_slug = parts[0], parts[1]
    if page_type == "market":
        selected = _get_json(f"{GAMMA_BASE_URL}/markets/slug/{page_slug}")
    else:
        event = _get_json(f"{GAMMA_BASE_URL}/events/slug/{page_slug}")
        markets = event.get("markets") or []
        requested_slug = market.market_slug or (parts[2] if len(parts) > 2 else "")
        if requested_slug:
            selected = next(
                (item for item in markets if item.get("slug") == requested_slug), None
            )
            if selected is None:
                raise ValueError(f"Market slug {requested_slug!r} was not found in the event")
        elif len(markets) == 1:
            selected = markets[0]
        else:
            available = ", ".join(str(item.get("slug") or "") for item in markets)
            raise ValueError(
                "This event contains multiple markets; set market_slug. "
                f"Available values: {available}"
            )

    _validate_tradeable(selected)
    outcomes = _json_list(selected.get("outcomes"), "outcomes")
    token_ids = _json_list(selected.get("clobTokenIds"), "clobTokenIds")
    if len(outcomes) != len(token_ids):
        raise ValueError("Gamma returned mismatched outcomes and CLOB token IDs")
    try:
        index = [str(value).casefold() for value in outcomes].index(market.outcome.casefold())
    except ValueError as error:
        raise ValueError(
            f"Outcome {market.outcome!r} is unavailable; choose one of {outcomes}"
        ) from error

    return replace(
        market,
        token_id=str(token_ids[index]),
        market_slug=str(selected.get("slug") or market.market_slug),
        condition_id=str(selected.get("conditionId") or ""),
        label=market.label or f"{selected.get('question', page_slug)} — {outcomes[index]}",
    )


def _get_json(url: str) -> dict:
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Gamma returned an unexpected response")
    return data


def _json_list(value: object, field: str) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not value:
        raise ValueError(f"Gamma response is missing {field}")
    return value


def _validate_tradeable(market: dict) -> None:
    if not market.get("active") or market.get("closed"):
        raise ValueError("The selected Polymarket market is not active")
    if not market.get("enableOrderBook") or not market.get("acceptingOrders"):
        raise ValueError("The selected Polymarket market is not accepting CLOB orders")
