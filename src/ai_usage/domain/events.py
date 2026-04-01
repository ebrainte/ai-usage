"""Domain events emitted by the application."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ai_usage.domain.models import Provider, UsageData


@dataclass(frozen=True)
class Event:
    """Base event class."""

    timestamp: datetime = field(default_factory=lambda: datetime.now().astimezone())


@dataclass(frozen=True)
class UsageRefreshed(Event):
    """Emitted when usage data is successfully fetched for an account."""

    account_id: str = ""
    data: UsageData | None = None


@dataclass(frozen=True)
class UsageFetchFailed(Event):
    """Emitted when usage data fetch fails."""

    account_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class AuthExpired(Event):
    """Emitted when authentication for an account has expired."""

    account_id: str = ""
    provider: Provider = Provider.CLAUDE


@dataclass(frozen=True)
class AccountAdded(Event):
    """Emitted when a new account is added."""

    account_id: str = ""


@dataclass(frozen=True)
class AccountRemoved(Event):
    """Emitted when an account is removed."""

    account_id: str = ""
