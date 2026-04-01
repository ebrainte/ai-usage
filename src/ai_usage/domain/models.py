"""Domain models for AI usage tracking.

These are pure data structures with no external dependencies.
They represent the core business concepts of the application.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Provider(StrEnum):
    """Supported AI service providers."""

    CLAUDE = "claude"
    CHATGPT = "chatgpt"
    COPILOT = "copilot"


class AuthMethod(StrEnum):
    """How credentials were obtained."""

    OAUTH_TOKEN = "oauth_token"
    SESSION_COOKIE = "session_cookie"
    API_KEY = "api_key"
    DEVICE_FLOW = "device_flow"
    MANUAL_TOKEN = "manual_token"


class AccountStatus(StrEnum):
    """Current status of an account."""

    ACTIVE = "active"
    AUTH_EXPIRED = "auth_expired"
    ERROR = "error"
    UNCONFIGURED = "unconfigured"


class Credential(BaseModel):
    """Stored credential for a provider account.

    The actual secret value is stored in keyring (macOS Keychain, etc.),
    NOT in this model. This just holds the metadata needed to retrieve it.
    """

    auth_method: AuthMethod
    keyring_key: str  # Key used to store/retrieve from keyring
    expires_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now().astimezone() >= self.expires_at


class Account(BaseModel):
    """A single provider account (user can have multiple per provider)."""

    id: str  # Unique ID (e.g., "claude-personal", "claude-work")
    provider: Provider
    label: str  # Human-friendly name
    credential: Credential | None = None
    status: AccountStatus = AccountStatus.UNCONFIGURED
    email: str | None = None
    last_refreshed: datetime | None = None

    @property
    def display_name(self) -> str:
        if self.email:
            return f"{self.label} ({self.email})"
        return self.label


class Quota(BaseModel):
    """A single usage quota (e.g., session limit, weekly limit)."""

    name: str  # e.g., "session", "weekly", "premium_requests"
    limit: float | None = None  # None = unlimited
    used: float = 0.0
    remaining: float | None = None
    unit: str = "requests"  # "requests", "tokens", "percent", "credits"
    reset_at: datetime | None = None

    @property
    def usage_percent(self) -> float:
        """Usage as a percentage (0-100)."""
        if self.limit is not None and self.limit > 0:
            return min(100.0, (self.used / self.limit) * 100.0)
        if self.remaining is not None and self.used > 0:
            total = self.used + self.remaining
            if total > 0:
                return min(100.0, (self.used / total) * 100.0)
        return 0.0

    @property
    def reset_in_human(self) -> str | None:
        """Human-readable time until reset."""
        if self.reset_at is None:
            return None
        now = datetime.now().astimezone()
        delta = self.reset_at - now
        if delta.total_seconds() <= 0:
            return "now"
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes = remainder // 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


class ModelBreakdown(BaseModel):
    """Usage breakdown per model (e.g., Opus vs Sonnet)."""

    model_name: str
    usage_percent: float = 0.0  # 0-100
    tokens_used: int | None = None


class UsageData(BaseModel):
    """Complete usage data for one account."""

    account_id: str
    provider: Provider
    plan_name: str | None = None
    quotas: list[Quota] = Field(default_factory=list)
    model_breakdown: list[ModelBreakdown] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    raw_data: dict[str, Any] = Field(default_factory=dict)  # Keep raw response for debugging
    error: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @property
    def primary_quota(self) -> Quota | None:
        """The most relevant quota to show as the main usage indicator."""
        if not self.quotas:
            return None
        # Prefer "session" > "weekly" > first available
        for name in ("session", "weekly", "daily", "monthly"):
            for q in self.quotas:
                if q.name.lower() == name:
                    return q
        return self.quotas[0]
