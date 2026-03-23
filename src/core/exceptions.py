"""
src/core/exceptions.py

Domain-specific exception hierarchy for poly-oracle-agent.
"""


class PolyOracleError(Exception):
    """Base exception for all poly-oracle-agent errors."""


class NonceManagerError(PolyOracleError):
    """Raised when the NonceManager encounters an RPC or state error."""

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class GasEstimatorError(PolyOracleError):
    """Raised when gas price exceeds the safety ceiling."""

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class DryRunActiveError(PolyOracleError):
    """Raised when a write operation is attempted with dry_run=True."""


class BroadcastError(PolyOracleError):
    """Raised on CLOB submission failure or receipt timeout."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.cause = cause


class ExposureLimitError(PolyOracleError):
    """Raised when a trade would exceed exposure or bankroll limits."""


class WebSocketError(PolyOracleError):
    """Raised on CLOB WebSocket connection failures."""

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class RESTClientError(PolyOracleError):
    """Raised on Gamma REST API failures."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.cause = cause
