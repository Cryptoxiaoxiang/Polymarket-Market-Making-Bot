import asyncio
import base64
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


def _authorization(password: str) -> str:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode()
    return f"Basic {encoded}"


def _read(request: Request) -> tuple[int, dict | str]:
    try:
        with urlopen(request, timeout=3) as response:
            body = response.read().decode()
            if response.headers.get_content_type() == "application/json":
                return response.status, json.loads(body)
            return response.status, body
    except HTTPError as error:
        return error.code, json.loads(error.read().decode())


def test_console_auth_status_and_pause_action(tmp_path) -> None:
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
            password="test-password",
            enabled=True,
        )
        console.start(asyncio.get_running_loop())
        try:
            assert console.address is not None
            origin = f"http://127.0.0.1:{console.address[1]}"

            unauthorized = Request(f"{origin}/api/status")
            status, payload = await asyncio.to_thread(_read, unauthorized)
            assert status == 401
            assert payload == {"error": "authentication required"}

            authorized = Request(
                f"{origin}/api/status",
                headers={"Authorization": _authorization("test-password")},
            )
            status, payload = await asyncio.to_thread(_read, authorized)
            assert status == 200
            assert payload["dry_run"] is True
            assert "private_key" not in payload

            pause = Request(
                f"{origin}/api/pause",
                method="POST",
                headers={
                    "Authorization": _authorization("test-password"),
                    "Origin": origin,
                    "X-Requested-With": "poly-mm-console",
                },
            )
            status, payload = await asyncio.to_thread(_read, pause)
            assert status == 200
            assert payload["status"]["paused"] is True
        finally:
            console.stop()

    asyncio.run(scenario())


def test_console_stays_disabled_without_password(tmp_path) -> None:
    engine = MarketMakerEngine(
        BotConfig(markets=[MarketConfig(token_id="token-1")]),
        ConsoleFakeClient(tmp_path / "orders.json"),
    )
    console = ConsoleServer(
        engine,
        host="127.0.0.1",
        port=0,
        password=None,
        enabled=True,
    )

    loop = asyncio.new_event_loop()
    console.start(loop)
    loop.close()

    assert console.address is None
