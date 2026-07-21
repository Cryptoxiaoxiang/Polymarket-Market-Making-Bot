from __future__ import annotations

import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from poly_mm.models import ManagedOrder


class OrderJournal:
    """Atomic local journal for orders created by this bot instance."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[ManagedOrder]:
        raw = self._load_payload()
        try:
            return [ManagedOrder.from_dict(item) for item in raw["orders"]]
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"Invalid order journal {self.path}: {error}") from error

    def load_quote_deadline(self) -> float | None:
        raw = self._load_payload()
        value = raw.get("quote_deadline_at")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"Invalid order journal {self.path}: invalid quote deadline")
        deadline = float(value)
        if not math.isfinite(deadline) or deadline <= 0:
            raise RuntimeError(f"Invalid order journal {self.path}: invalid quote deadline")
        return deadline

    def _load_payload(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "orders": [], "quote_deadline_at": None}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(raw, dict)
                or raw.get("version") != 1
                or not isinstance(raw.get("orders"), list)
            ):
                raise ValueError("unsupported journal format")
            return raw
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Invalid order journal {self.path}: {error}") from error

    def save(
        self,
        orders: list[ManagedOrder],
        *,
        quote_deadline_at: float | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "orders": [order.to_dict() for order in orders],
            "quote_deadline_at": quote_deadline_at,
        }
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self.path)

    def remove(self) -> None:
        if self.path.exists():
            self.save([])
