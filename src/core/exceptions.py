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


class BalanceFetchError(PolyOracleError):
    """Raised when the live bankroll balance cannot be fetched safely."""

    def __init__(
        self,
        reason: str,
        wallet_address: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        message = reason
        if wallet_address:
            message = f"{reason} (wallet_address={wallet_address})"
        super().__init__(message)
        self.reason = reason
        self.wallet_address = wallet_address
        self.cause = cause


class RoutingRejectedError(PolyOracleError):
    """Raised when a validated decision cannot be routed into an order."""

    def __init__(
        self,
        reason: str,
        token_id: str | None = None,
        **context: object,
    ) -> None:
        message = reason
        if token_id:
            message = f"{reason} (token_id={token_id})"
        super().__init__(message)
        self.reason = reason
        self.token_id = token_id
        self.context = context


class RoutingAbortedError(PolyOracleError):
    """Raised when an upstream dependency aborts the routing attempt."""

    def __init__(
        self,
        reason: str,
        token_id: str | None = None,
        cause: Exception | None = None,
        **context: object,
    ) -> None:
        message = reason
        if token_id:
            message = f"{reason} (token_id={token_id})"
        super().__init__(message)
        self.reason = reason
        self.token_id = token_id
        self.cause = cause
        self.context = context


class SlippageExceededError(RoutingAbortedError):
    """Raised when the best ask exceeds the configured slippage tolerance."""


class ExitEvaluationError(PolyOracleError):
    """Raised when exit strategy evaluation fails."""

    def __init__(
        self,
        reason: str,
        position_id: str | None = None,
        condition_id: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        message = reason
        if position_id:
            message = f"{message} (position_id={position_id})"
        if condition_id:
            message = f"{message} (condition_id={condition_id})"
        super().__init__(message)
        self.reason = reason
        self.position_id = position_id
        self.condition_id = condition_id
        self.cause = cause


class ExitMutationError(PolyOracleError):
    """Raised when position state transition fails."""

    def __init__(
        self,
        reason: str,
        position_id: str | None = None,
        condition_id: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        message = reason
        if position_id:
            message = f"{message} (position_id={position_id})"
        if condition_id:
            message = f"{message} (condition_id={condition_id})"
        super().__init__(message)
        self.reason = reason
        self.position_id = position_id
        self.condition_id = condition_id
        self.cause = cause


class ExitRoutingError(PolyOracleError):
    """Raised when exit order routing fails."""

    def __init__(
        self,
        reason: str,
        position_id: str | None = None,
        condition_id: str | None = None,
        cause: Exception | None = None,
        **context: object,
    ) -> None:
        message = reason
        if position_id:
            message = f"{message} (position_id={position_id})"
        if condition_id:
            message = f"{message} (condition_id={condition_id})"
        super().__init__(message)
        self.reason = reason
        self.position_id = position_id
        self.condition_id = condition_id
        self.cause = cause
        self.context = context


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
