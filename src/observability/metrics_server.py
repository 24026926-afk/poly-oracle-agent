"""
src/observability/metrics_server.py

Lightweight asyncio HTTP server exposing GET /metrics in Prometheus text
exposition format.

Uses the Python standard library only — no web frameworks.
Designed to complement (or be merged with) the WI-46 HealthServer.
"""

import asyncio
from typing import Optional

import structlog

from src.observability.metrics import MetricsRegistry, MetricsSnapshot

logger = structlog.get_logger(__name__)

_CONTENT_TYPE = b"Content-Type: text/plain; charset=utf-8\r\n"
_HTTP_200_LINE = b"HTTP/1.1 200 OK\r\n"
_HTTP_500_LINE = b"HTTP/1.1 500 Internal Server Error\r\n"
_NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
_ERROR_BODY = b"# Metrics unavailable\n"


class MetricsServer:
    """Local asyncio HTTP server exposing GET /metrics.

    Accepts a ``MetricsRegistry`` so the Orchestrator can wire a single
    shared registry across all runtime layers.
    """

    def __init__(
        self,
        registry: MetricsRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 8081,
    ) -> None:
        self._registry = registry
        self._host = host
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None

    # ── Public lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """Bind and start the metrics HTTP server."""
        try:
            self._server = await asyncio.start_server(
                self._handle_request,
                host=self._host,
                port=self._port,
            )
        except OSError as exc:
            logger.error(
                "metrics_server.port_conflict",
                host=self._host,
                port=self._port,
                error=str(exc),
            )
            raise

        logger.info(
            "metrics_server.started",
            host=self._host,
            port=self._port,
        )

    async def stop(self) -> None:
        """Stop the metrics server and clean up pending connections."""
        if self._server is None:
            return

        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("metrics_server.stopped")

    # ── Internal — request routing ─────────────────────────────────────

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
            writer.close()
            return

        method, path = parts[0], parts[1]

        # Drain headers
        while True:
            header_line = await reader.readline()
            if header_line in (b"\r\n", b"\n", b""):
                break

        if method != "GET":
            await self._respond_text(
                writer, b"HTTP/1.1 405 Method Not Allowed\r\n\r\n"
            )
            return

        if path == "/metrics":
            await self._handle_metrics(writer)
        else:
            writer.write(_NOT_FOUND)
            await writer.drain()
            writer.close()

    async def _handle_metrics(self, writer: asyncio.StreamWriter) -> None:
        """Render Prometheus text exposition and write the response."""
        try:
            snapshot: MetricsSnapshot = await self._registry.snapshot()
            text = self._registry.render_prometheus(snapshot)
            body = text.encode("utf-8")
        except Exception:
            logger.exception("metrics_server.render_failed")
            writer.write(_HTTP_500_LINE)
            writer.write(_CONTENT_TYPE)
            writer.write(f"Content-Length: {len(_ERROR_BODY)}\r\n\r\n".encode("ascii"))
            writer.write(_ERROR_BODY)
            await writer.drain()
            writer.close()
            return

        writer.write(_HTTP_200_LINE)
        writer.write(_CONTENT_TYPE)
        writer.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        )
        writer.write(body)
        await writer.drain()
        writer.close()

    @staticmethod
    async def _respond_text(
        writer: asyncio.StreamWriter, response: bytes
    ) -> None:
        """Write a raw response and close."""
        try:
            writer.write(response)
            writer.write(b"\r\n")
            await writer.drain()
        finally:
            writer.close()
