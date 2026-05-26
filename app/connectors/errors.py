class ProviderError(Exception):
    def __init__(
        self, message: str, error_code: str = "PROVIDER_ERROR", retryable: bool = False
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class SessionExpiredError(ProviderError):
    """Session handshake token stale (e.g. Transmission 409). Triggers refresh + retry-once."""

    def __init__(
        self, message: str = "Session expired", error_code: str = "SESSION_EXPIRED"
    ):
        super().__init__(message, error_code=error_code, retryable=True)


class AuthExpiredError(ProviderError):
    """Credential/access token expired (e.g. OAuth 401). Triggers refresh + retry-once."""

    def __init__(self, message: str = "Auth expired", error_code: str = "AUTH_EXPIRED"):
        super().__init__(message, error_code=error_code, retryable=True)


class RateLimitedError(ProviderError):
    """Provider 429. retry_after seconds honored by the transient-retry loop."""

    def __init__(self, retry_after: float | None = None, message: str = "Rate limited"):
        super().__init__(message, error_code="RATE_LIMITED", retryable=True)
        self.retry_after = retry_after
