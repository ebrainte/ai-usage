"""Storage port — interface for persisting account configurations."""

from __future__ import annotations

from typing import Protocol

from ai_usage.domain.models import Account


class StoragePort(Protocol):
    """Persists and retrieves account configurations.

    The storage port handles account metadata (provider, label, credential refs).
    Actual secrets go to keyring — this only stores the keyring key references.
    """

    def load_accounts(self) -> list[Account]:
        """Load all configured accounts.

        Returns:
            List of Account objects (credentials may reference keyring keys).
        """
        ...

    def save_account(self, account: Account) -> None:
        """Save or update an account configuration.

        Args:
            account: The account to save.
        """
        ...

    def delete_account(self, account_id: str) -> None:
        """Delete an account configuration.

        Args:
            account_id: The ID of the account to delete.
        """
        ...

    def save_all(self, accounts: list[Account]) -> None:
        """Save all accounts (full replace).

        Args:
            accounts: Complete list of accounts to persist.
        """
        ...
