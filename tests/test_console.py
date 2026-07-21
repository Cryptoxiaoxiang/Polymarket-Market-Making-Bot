import asyncio
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from poly_mm.config import BotConfig, MarketConfig
from poly_mm.console import ConsoleServer
from poly_mm.engine import MarketMakerEngine


class ConsoleFakeClient:
    def __init__(self, journal_path) -> None:
        self.settings = SimpleNamespace(order_journal_path=str(journal_path))
        self.websocket_connected = False

    def get_open_orders(self, token_id=None) -> list[dict]:
        return []


def _read(request: Request) -> tuple[int, dict | str]:
    try:
        with urlopen(request, timeout=3) as response:
            body = response.read().decode()
            if response.headers.get_content_type() == "application/json":
                return response.status, json.loads(body)
            return response.status, body
    except HTTPError as error:
        return error.code, json.loads(error.read().decode())


def test_console_status_and_pause_action_without_login(tmp_path) -> None:
    async def scenario() -> None:
        engine = MarketMakerEngine(
            BotConfig(
                dry_run=True,
                markets=[MarketConfig(token_id="token-1", condition_id="condition-1")],
            ),
            ConsoleFakeClient(tmp_path / "orders.json"),
        )
        console = ConsoleServer(
            engine,
            host="127.0.0.1",
            port=0,
            enabled=True,
        )
        console.start(asyncio.get_running_loop())
        try:
            assert console.address is not None
            origin = f"http://127.0.0.1:{console.address[1]}"

            status_request = Request(f"{origin}/api/status")
            status, payload = await asyncio.to_thread(_read, status_request)
            assert status == 200
            assert payload["dry_run"] is True
            assert "private_key" not in payload

            rejected = Request(f"{origin}/api/pause", method="POST")
            status, payload = await asyncio.to_thread(_read, rejected)
            assert status == 403
            assert payload == {"error": "invalid control request"}

            pause = Request(
                f"{origin}/api/pause",
                method="POST",
                headers={
                    "Origin": origin,
                    "X-Requested-With": "poly-mm-console",
                },
            )
            status, payload = await asyncio.to_thread(_read, pause)
            assert status == 200
            assert payload["status"]["paused"] is True

            invalid_expiry = Request(
                f"{origin}/api/expiry",
                data=json.dumps({"hours": 0, "minutes": 0}).encode(),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "X-Requested-With": "poly-mm-console",
                },
            )
            status, payload = await asyncio.to_thread(_read, invalid_expiry)
            assert status == 400
            assert "1 minute" in payload["error"]

            expiry = Request(
                f"{origin}/api/expiry",
                data=json.dumps({"hours": 2, "minutes": 15}).encode(),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "X-Requested-With": "poly-mm-console",
                },
            )
            status, payload = await asyncio.to_thread(_read, expiry)
            assert status == 200
            assert payload["status"]["paused"] is False
            assert 8_090 <= payload["status"]["quote_task"]["remaining_seconds"] <= 8_100

            clear = Request(
                f"{origin}/api/expiry/clear",
                method="POST",
                headers={
                    "Origin": origin,
                    "X-Requested-With": "poly-mm-console",
                },
            )
            status, payload = await asyncio.to_thread(_read, clear)
            assert status == 200
            assert payload["status"]["quote_task"]["deadline_at"] is None
        finally:
            console.stop()

    asyncio.run(scenario())


def test_console_stays_disabled_when_config_disabled(tmp_path) -> None:
    engine = MarketMakerEngine(
        BotConfig(markets=[MarketConfig(token_id="token-1")]),
        ConsoleFakeClient(tmp_path / "orders.json"),
    )
    console = ConsoleServer(
        engine,
        host="127.0.0.1",
        port=0,
        enabled=False,
    )

    loop = asyncio.new_event_loop()
    console.start(loop)
    loop.close()

    assert console.address is None
