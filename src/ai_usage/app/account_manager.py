"""Account manager — CRUD operations for provider accounts.

Handles adding, removing, and configuring accounts across all providers.
Each account gets its own credential stored independently in keyring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ai_usage.adapters.claude.auth import ClaudeAuth
from ai_usage.adapters.copilot.auth import CopilotAuth
from ai_usage.adapters.chatgpt.auth import ChatGPTAuth
from ai_usage.adapters.storage.file import FileStorage
from ai_usage.domain.models import Account, AccountStatus, Credential, Provider

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class AccountManager:
    """Manages provider accounts and their credentials."""

    def __init__(self, storage: FileStorage | None = None):
        self.storage = storage or FileStorage()
        self._auth_handlers = {
            Provider.CLAUDE: ClaudeAuth(),
            Provider.COPILOT: CopilotAuth(),
            Provider.CHATGPT: ChatGPTAuth(),
        }

    def list_accounts(self) -> list[Account]:
        """List all configured accounts."""
        return self.storage.load_accounts()

    def get_account(self, account_id: str) -> Account | None:
        """Get a specific account by ID."""
        accounts = self.storage.load_accounts()
        return next((a for a in accounts if a.id == account_id), None)

    def add_account(
        self,
        provider: Provider,
        label: str,
        account_id: str | None = None,
    ) -> Account:
        """Add a new account (without credentials — login separately)."""
        if not account_id:
            # Generate ID from provider + label, sanitized for use as widget IDs
            import re

            slug = label.lower()
            slug = slug.replace("@", "-at-").replace(".", "-")
            slug = re.sub(r"[^a-z0-9-]", "-", slug)
            slug = re.sub(r"-+", "-", slug).strip("-")
            account_id = f"{provider.value}-{slug}"

        # Check for duplicate ID
        existing = self.get_account(account_id)
        if existing:
            raise ValueError(f"Account with ID '{account_id}' already exists")

        account = Account(
            id=account_id,
            provider=provider,
            label=label,
            status=AccountStatus.UNCONFIGURED,
        )
        self.storage.save_account(account)
        logger.info("Added account: %s (%s)", account.label, account.provider)
        return account

    def remove_account(self, account_id: str) -> bool:
        """Remove an account and clean up its credentials."""
        account = self.get_account(account_id)
        if not account:
            return False

        self.storage.delete_account(account_id)
        logger.info("Removed account: %s", account_id)
        return True

    def update_account(self, account: Account) -> None:
        """Update an account's data."""
        self.storage.save_account(account)

    async def login(self, account_id: str) -> Account:
        """Authenticate an account using the provider's auth strategies.

        Returns the updated account with valid credentials.
        """
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"Account '{account_id}' not found")

        auth = self._auth_handlers.get(account.provider)
        if not auth:
            raise ValueError(f"No auth handler for provider: {account.provider}")

        credential = await auth.authenticate(account)
        account.credential = credential
        account.status = AccountStatus.ACTIVE
        self.storage.save_account(account)

        logger.info("Authenticated account: %s", account.display_name)
        return account

    async def login_with_session_key(self, account_id: str, session_key: str) -> Account:
        """Login a Claude account with a session key."""
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"Account '{account_id}' not found")

        if account.provider == Provider.CLAUDE:
            auth = self._auth_handlers[Provider.CLAUDE]
            credential = await auth.authenticate_with_session_key(account, session_key)
        elif account.provider == Provider.CHATGPT:
            auth = self._auth_handlers[Provider.CHATGPT]
            credential = await auth.authenticate_with_session_token(account, session_key)
        else:
            raise ValueError(f"Session key login not supported for {account.provider}")

        account.credential = credential
        account.status = AccountStatus.ACTIVE
        self.storage.save_account(account)
        return account

    async def login_with_token(self, account_id: str, token: str) -> Account:
        """Login an account with a token (GitHub PAT, API key, etc.)."""
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"Account '{account_id}' not found")

        if account.provider == Provider.COPILOT:
            auth = self._auth_handlers[Provider.COPILOT]
            credential = await auth.authenticate_with_token(account, token)
        elif account.provider == Provider.CLAUDE:
            auth = self._auth_handlers[Provider.CLAUDE]
            credential = await auth.authenticate_with_oauth_token(account, token)
        else:
            raise ValueError(f"Token login not supported for {account.provider}")

        account.credential = credential
        account.status = AccountStatus.ACTIVE
        self.storage.save_account(account)
        return account

    async def login_copilot_device_flow(
        self, account_id: str, on_user_code: callable | None = None
    ) -> Account:
        """Login a Copilot account via GitHub device flow."""
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"Account '{account_id}' not found")

        if account.provider != Provider.COPILOT:
            raise ValueError("Device flow login only supported for Copilot")

        auth = self._auth_handlers[Provider.COPILOT]
        credential = await auth.authenticate_with_device_flow(account, on_user_code=on_user_code)

        account.credential = credential
        account.status = AccountStatus.ACTIVE
        self.storage.save_account(account)
        return account

    async def login_claude_browser(
        self,
        account_id: str,
        on_url: callable | None = None,
    ) -> Account:
        """Login a Claude account via OAuth PKCE browser flow."""
        account = self.get_account(account_id)
        if not account:
            raise ValueError(f"Account '{account_id}' not found")

        if account.provider != Provider.CLAUDE:
            raise ValueError("Browser OAuth login only supported for Claude")

        auth = self._auth_handlers[Provider.CLAUDE]
        credential = await auth.authenticate_with_browser(account, on_url=on_url)

        account.credential = credential
        account.status = AccountStatus.ACTIVE
        self.storage.save_account(account)
        return account

    async def validate_all(self) -> dict[str, bool]:
        """Validate credentials for all accounts.

        Returns: dict of account_id -> is_valid
        """
        results = {}
        for account in self.list_accounts():
            auth = self._auth_handlers.get(account.provider)
            if auth:
                results[account.id] = await auth.validate_credential(account)
            else:
                results[account.id] = False
        return results

    def get_auth_handler(self, provider: Provider):
        """Get the auth handler for a provider."""
        return self._auth_handlers.get(provider)
