from decimal import Decimal

from poly_mm.journal import OrderJournal
from poly_mm.models import ExitIntent, ManagedOrder, Quote, Side


def test_journal_round_trip_and_private_permissions(tmp_path) -> None:
    path = tmp_path / "orders.json"
    journal = OrderJournal(path)
    order = ManagedOrder(
        "order-1",
        Quote("token-1", Side.BUY, Decimal("0.42"), Decimal("5")),
        1_700_000_000,
        Decimal("1.5"),
    )

    intent = ExitIntent(
        "order-1:1.5",
        "order-1",
        "token-1",
        Decimal("1.5"),
        False,
        1_700_000_001,
        Decimal("0.01"),
    )
    journal.save([order], quote_deadline_at=1_800_000_000, pending_exits=[intent])
    restored = journal.load()

    assert restored == [order]
    assert journal.load_quote_deadline() == 1_800_000_000
    assert journal.load_pending_exits() == [intent]
    assert path.stat().st_mode & 0o777 == 0o600
