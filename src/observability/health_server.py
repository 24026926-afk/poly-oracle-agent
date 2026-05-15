"""
src/observability/health_server.py

Lightweight asyncio HTTP server exposing /healthz and /readyz endpoints.

Uses the Python standard library only — no web frameworks.
Designed for 24/7 dry-run observability per WI-46.
"""

import asyncio
import json
from collections.abc import Callable, Awaitable
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.observability.health import (
    HealthEndpointResponse,
    ReadinessStatus,
    RuntimeHealthSnapshot,
    WebSocketConnectionState,
)

logger = structlog.get_logger(__name__)

_HEALTHZ_BODY = json.dumps({"status": "ok"}).encode("utf-8")
_HTTP_200 = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
_HTTP_503 = b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: "
_NL2 = b"\r\n\r\n"


class HealthServer:
    """Local asyncio HTTP health server with /healthz and /readyz.

    Accepts async callables for DB and WS health checks so the Orchestrator
    can inject real dependencies without coupling to agent internals.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        *,
        get_ws_health: Callable[
            [], Awaitable[RuntimeHealthSnapshot]
        ]
        | None = None,
        check_db: Callable[[], Awaitable[bool]] | None = None,
        readiness_grace_window_seconds: float = 30.0,
        dry_run: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._get_ws_health = get_ws_health
        self._check_db = check_db
        self._grace_window_s = readiness_grace_window_seconds
        self._dry_run = dry_run
        self._server: Optional[asyncio.AbstractServer] = None
        self._started_at_utc = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind and start the health HTTP server."""
        try:
            self._server = await asyncio.start_server(
                self._handle_request,
                host=self._host,
                port=self._port,
            )
        except OSError as exc:
            logger.error(
                "health_server.port_conflict",
                host=self._host,
                port=self._port,
                error=str(exc),
            )
            raise

        logger.info(
            "health_server.started",
            host=self._host,
            port=self._port,
        )

    async def stop(self) -> None:
        """Stop the health server and clean up pending connections."""
        if self._server is None:
            return

        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("health_server.stopped")

    # ------------------------------------------------------------------
    # Internal — request routing
    # ------------------------------------------------------------------

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Parse HTTP request line and route to handler."""
        try:
            request_line = await asyncio.wait_for(
                reader.readline(), timeout=5.0
            )
        except asyncio.TimeoutError:
            writer.close()
            return

        if not request_line:
            writer.close()
            return

        decoded = request_line.decode("utf-8", errors="replace").strip()
        parts = decoded.split(" ")

        if len(parts) < 2:
            await self._respond(writer, 400, {"error": "bad_request"})
            return

        method, path = parts[0], parts[1]

        # Drain headers (not needed for health endpoints)
        while True:
            header_line = await reader.readline()
            if header_line in (b"\r\n", b"\n", b""):
                break

        if method != "GET":
            await self._respond(writer, 405, {"error": "method_not_allowed"})
            return

        if path == "/healthz":
            await self._handle_healthz(writer)
        elif path == "/readyz":
            await self._handle_readyz(writer)
        else:
            await self._respond(writer, 404, {"error": "not_found"})

    async def _handle_healthz(self, writer: asyncio.StreamWriter) -> None:
        """Liveness probe — always 200 if the event loop is alive."""
        try:
            writer.write(_HTTP_200)
            writer.write(str(len(_HEALTHZ_BODY)).encode("ascii"))
            writer.write(_NL2)
            writer.write(_HEALTHZ_BODY)
            await writer.drain()
        finally:
            writer.close()

    async def _handle_readyz(self, writer: asyncio.StreamWriter) -> None:
        """Readiness probe — checks DB reachability and WS health."""
        db_ok = True
        ws_ok = True
        ws_connected = False
        ws_state: Optional[WebSocketConnectionState] = None

        if self._check_db is not None:
            try:
                db_ok = await self._check_db()
            except Exception:
                db_ok = False

        ws_health = None
        runtime = None
        if self._get_ws_health is not None:
            try:
                runtime = await self._get_ws_health()
                ws_health = runtime.ws_health if runtime else None
                if ws_health is not None:
                    ws_state = ws_health.connection_state
                    ws_connected = (
                        ws_state == WebSocketConnectionState.CONNECTED
                    )
                    # Grace window: if recently disconnected and DB is ok, still ready
                    if (
                        not ws_connected
                        and db_ok
                        and ws_state
                        == WebSocketConnectionState.DISCONNECTED
                        and ws_health.last_pong_received_at_utc is not None
                    ):
                        delta = (
                            datetime.now(timezone.utc)
                            - ws_health.last_pong_received_at_utc
                        )
                        if delta.total_seconds() <= self._grace_window_s:
                            ws_ok = True  # within grace window
                        else:
                            ws_ok = False
                    elif not ws_connected:
                        ws_ok = False
                    else:
                        ws_ok = True
            except Exception:
                ws_ok = False

        # Determine readiness
        # WI-56: ledger degraded forces DEGRADED even if everything else is ok
        ledger_ok = not (
            runtime is not None
            and getattr(runtime, "ledger_degraded", False)
        )

        if db_ok and ws_ok and ledger_ok:
            status = ReadinessStatus.READY
            http_status = 200
        elif db_ok and (not ws_ok or not ledger_ok):
            status = ReadinessStatus.DEGRADED
            http_status = 200
        else:
            status = ReadinessStatus.NOT_READY
            http_status = 503

        checks: dict[str, str] = {
            "database": "reachable" if db_ok else "unreachable",
            "websocket": ws_state.value if ws_state else "unknown",
        }

        response = HealthEndpointResponse(
            status=status.value,
            checks=checks,
            dry_run=self._dry_run,
        )

        body = response.model_dump_json().encode("utf-8")
        await self._respond(writer, http_status, body, raw_body=True)

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status_code: int,
        body: object,
        raw_body: bool = False,
    ) -> None:
        """Write an HTTP response and close the connection."""
        try:
            if raw_body and isinstance(body, bytes):
                payload = body
            else:
                payload = json.dumps(body).encode("utf-8")

            status_line = (
                b"HTTP/1.1 200 OK\r\n"
                if status_code == 200
                else f"HTTP/1.1 {status_code} \r\n".encode("ascii")
            )
            if status_code == 503:
                status_line = _HTTP_503
            elif status_code == 200 and raw_body:
                status_line = _HTTP_200

            writer.write(status_line)
            writer.write(
                f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n".encode(
                    "ascii"
                )
            )
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
