"""File-based storage adapter for account configurations.

Stores account metadata in YAML at ~/.config/ai-usage/accounts.yaml.
Actual secrets are stored in keyring (macOS Keychain, etc.).
"""

from __future__ import annotations

import logging
from pathlib import Path

import keyring
import yaml
from pydantic import TypeAdapter

from ai_usage.domain.models import Account

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "ai-usage"
ACCOUNTS_FILE = "accounts.yaml"

# Type adapter for serializing/deserializing lists of Account
_accounts_adapter = TypeAdapter(list[Account])


class FileStorage:
    """File-based storage using YAML + keyring for secrets."""

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        self.accounts_path = self.config_dir / ACCOUNTS_FILE
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def load_accounts(self) -> list[Account]:
        """Load all configured accounts from YAML."""
        if not self.accounts_path.exists():
            return []

        try:
            raw = self.accounts_path.read_text()
            if not raw.strip():
                return []
            data = yaml.safe_load(raw)
            if not isinstance(data, list):
                logger.warning("Invalid accounts file format, expected list")
                return []
            return _accounts_adapter.validate_python(data)
        except Exception:
            logger.exception("Failed to load accounts from %s", self.accounts_path)
            return []

    def save_account(self, account: Account) -> None:
        """Save or update a single account."""
        accounts = self.load_accounts()
        # Replace if exists, append if new
        found = False
        for i, existing in enumerate(accounts):
            if existing.id == account.id:
                accounts[i] = account
                found = True
                break
        if not found:
            accounts.append(account)
        self.save_all(accounts)

    def delete_account(self, account_id: str) -> None:
        """Delete an account and its keyring credential."""
        accounts = self.load_accounts()
        account = next((a for a in accounts if a.id == account_id), None)

        # Clean up keyring secret
        if account and account.credential:
            try:
                keyring.delete_password("ai-usage", account.credential.keyring_key)
            except keyring.errors.PasswordDeleteError:
                pass  # Already gone

        accounts = [a for a in accounts if a.id != account_id]
        self.save_all(accounts)

    def save_all(self, accounts: list[Account]) -> None:
        """Save all accounts (full replace)."""
        self._ensure_dir()
        data = _accounts_adapter.dump_python(accounts, mode="json")
        self.accounts_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        logger.debug("Saved %d accounts to %s", len(accounts), self.accounts_path)


# --- Keyring helpers ---

KEYRING_SERVICE = "ai-usage"


def store_secret(key: str, value: str) -> None:
    """Store a secret in the system keyring."""
    keyring.set_password(KEYRING_SERVICE, key, value)


def get_secret(key: str) -> str | None:
    """Retrieve a secret from the system keyring."""
    return keyring.get_password(KEYRING_SERVICE, key)


def delete_secret(key: str) -> None:
    """Delete a secret from the system keyring."""
    try:
        keyring.delete_password(KEYRING_SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass
