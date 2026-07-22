from __future__ import annotations

import asyncio
import json
import logging
import threading
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Coroutine
from urllib.parse import urlsplit

logger = logging.getLogger("poly-mm")
STATIC_DIR = Path(__file__).resolve().parent / "web_static"


class _ConsoleHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class ConsoleServer:
    """Loopback-only operator console with same-origin control actions."""

    def __init__(
        self,
        controller: object,
        *,
        host: str,
        port: int,
        enabled: bool,
    ) -> None:
        self.controller = controller
        self.host = host
        self.port = port
        self.enabled = enabled
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: _ConsoleHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self.enabled:
            logger.info("Local web console is disabled")
            return
        self.loop = loop
        handler = partial(_ConsoleHandler, console=self)
        self._server = _ConsoleHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="poly-mm-console",
            daemon=True,
        )
        self._thread.start()
        logger.info("Local web console listening on http://%s:%s", *self.address)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def run(self, coroutine: Coroutine, timeout: float = 30) -> dict:
        if self.loop is None or not self.loop.is_running():
            coroutine.close()
            raise RuntimeError("Engine event loop is unavailable")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=timeout)


class _ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "PolyMMConsole/1"

    def __init__(self, *args, console: ConsoleServer, **kwargs) -> None:
        self.console = console
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif path == "/static/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
        elif path == "/static/app.js":
            self._send_static("app.js", "text/javascript; charset=utf-8")
        elif path == "/api/status":
            try:
                self._send_json(
                    HTTPStatus.OK,
                    self.console.run(self.console.controller.snapshot(), 5),
                )
            except Exception as error:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        elif path == "/api/logs":
            log_lines = getattr(self.console.controller, "log_lines", None)
            lines = log_lines() if callable(log_lines) else []
            self._send_json(HTTPStatus.OK, {"lines": lines})
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._valid_control_request():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid control request"})
            return
        path = urlsplit(self.path).path
        if path == "/api/account":
            self._controller_payload_action("save_account", timeout=45)
            return
        if path == "/api/start":
            self._controller_action("start_bot", timeout=45)
            return
        if path == "/api/stop":
            self._controller_action("stop_bot")
            return
        if path == "/api/preflight":
            self._controller_action("run_preflight", timeout=45)
            return
        if path == "/api/expiry":
            try:
                payload = self._read_json_body()
                result = self.console.run(
                    self.console.controller.set_quote_expiry(
                        payload.get("hours"), payload.get("minutes")
                    )
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "status": result})
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception as error:
                logger.warning("Console expiry action failed: %s", error)
                self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        if path == "/api/expiry/clear":
            try:
                result = self.console.run(self.console.controller.clear_quote_expiry())
                self._send_json(HTTPStatus.OK, {"ok": True, "status": result})
            except Exception as error:
                logger.warning("Console clear-expiry action failed: %s", error)
                self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        actions = {
            "/api/pause": self.console.controller.pause_quotes,
            "/api/resume": self.console.controller.resume_quotes,
        }
        action = actions.get(path)
        if action is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            result = self.console.run(action())
            self._send_json(HTTPStatus.OK, {"ok": True, "status": result})
        except Exception as error:
            logger.warning("Console action %s failed: %s", path, error)
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _controller_action(self, name: str, timeout: float = 30) -> None:
        action = getattr(self.console.controller, name, None)
        if action is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            result = self.console.run(action(), timeout)
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            logger.warning("Console controller action %s failed: %s", name, error)
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _controller_payload_action(self, name: str, timeout: float = 30) -> None:
        try:
            payload = self._read_json_body(maximum=8192)
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        action = getattr(self.console.controller, name, None)
        if action is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            result = self.console.run(action(payload), timeout)
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            logger.warning("Console controller payload action %s failed", name)
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _read_json_body(self, maximum: int = 2048) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request length") from error
        if not 1 <= length <= maximum:
            raise ValueError("Invalid request body")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ValueError("Invalid JSON body") from error
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _valid_control_request(self) -> bool:
        if self.headers.get("X-Requested-With") != "poly-mm-console":
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        return not origin or (bool(host) and origin == f"http://{host}")

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _send_static(self, filename: str, content_type: str) -> None:
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send(HTTPStatus.OK, body, content_type)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args) -> None:
        logger.debug("Console %s - %s", self.address_string(), format % args)
