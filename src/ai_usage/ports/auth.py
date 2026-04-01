"""Auth port — interface for authenticating with a provider."""

from __future__ import annotations

from typing import Protocol

from ai_usage.domain.models import Account, AuthMethod, Credential


class AuthPort(Protocol):
    """Handles authentication for a provider.

    Each provider adapter implements this with its own auth strategies
    (OAuth, cookies, API keys, device flow, etc.).
    """

    async def authenticate(self, account: Account) -> Credential:
        """Authenticate and return a credential.

        Tries auth strategies in order of preference until one succeeds.

        Args:
            account: The account to authenticate.

        Returns:
            A Credential with the keyring key where the secret is stored.

        Raises:
            AuthenticationError: If all auth strategies fail.
        """
        ...

    async def refresh_credential(self, account: Account) -> Credential:
        """Refresh an expired credential.

        Args:
            account: The account whose credential needs refreshing.

        Returns:
            A new Credential.

        Raises:
            AuthenticationError: If refresh fails.
        """
        ...

    def supported_auth_methods(self) -> list[AuthMethod]:
        """Return the auth methods this adapter supports, in priority order."""
        ...

    async def validate_credential(self, account: Account) -> bool:
        """Check if the current credential is still valid.

        Returns:
            True if the credential works, False otherwise.
        """
        ...
