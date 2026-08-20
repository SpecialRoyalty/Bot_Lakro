from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


LOGGER = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, port: int, payload_factory: Callable[[], dict[str, Any]]) -> None:
        self.port = port
        self.payload_factory = payload_factory
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        payload_factory = self.payload_factory

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - method name imposed by BaseHTTPRequestHandler
                if self.path not in {"/", "/health"}:
                    self.send_error(404)
                    return
                body = json.dumps(payload_factory(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="health-server", daemon=True)
        self._thread.start()
        LOGGER.info("Serveur de santé démarré sur le port %s", self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

