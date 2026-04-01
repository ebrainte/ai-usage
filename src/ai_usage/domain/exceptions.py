"""Application-level exceptions."""


class AiUsageError(Exception):
    """Base exception for ai-usage."""


class AuthenticationError(AiUsageError):
    """Authentication failed or credentials expired."""

    def __init__(self, provider: str, message: str, account_id: str | None = None):
        self.provider = provider
        self.account_id = account_id
        super().__init__(f"[{provider}] {message}")


class FetchError(AiUsageError):
    """Failed to fetch usage data from a provider."""

    def __init__(self, provider: str, message: str, status_code: int | None = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"[{provider}] {message}")


class ConfigError(AiUsageError):
    """Configuration error."""
