"""Usage port — interface for fetching usage data from a provider."""

from __future__ import annotations

from typing import Protocol

from ai_usage.domain.models import Account, UsageData


class UsagePort(Protocol):
    """Fetches usage data for a given account.

    Each provider adapter implements this protocol.
    """

    async def fetch_usage(self, account: Account) -> UsageData:
        """Fetch current usage data for the account.

        Args:
            account: The account to fetch usage for. Must have valid credentials.

        Returns:
            UsageData with quotas, plan info, and model breakdowns.

        Raises:
            AuthenticationError: If credentials are invalid or expired.
            FetchError: If the API call fails for any other reason.
        """
        ...

    def supports_provider(self) -> str:
        """Return the Provider value this adapter handles."""
        ...
