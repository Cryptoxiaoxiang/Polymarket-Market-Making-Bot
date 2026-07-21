from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from poly_mm.models import ManagedOrder


class OrderJournal:
    """Atomic local journal for orders created by this bot instance."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[ManagedOrder]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != 1 or not isinstance(raw.get("orders"), list):
                raise ValueError("unsupported journal format")
            return [ManagedOrder.from_dict(item) for item in raw["orders"]]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Invalid order journal {self.path}: {error}") from error

    def save(self, orders: list[ManagedOrder]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "orders": [order.to_dict() for order in orders]}
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
