"""ChatGPT authentication adapter.

Supports:
1. Browser cookie import (session tokens from chatgpt.com)
2. Manual session token (user pastes their access token)

Note: ChatGPT does NOT have a standard OAuth/API key flow for subscription
usage. Authentication relies on web session tokens.
"""

from __future__ import annotations

import json
import logging

import httpx

from ai_usage.adapters.storage.file import get_secret, store_secret
from ai_usage.domain.exceptions import AuthenticationError
from ai_usage.domain.models import Account, AuthMethod, Credential

logger = logging.getLogger(__name__)

CHATGPT_BASE = "https://chatgpt.com"


class ChatGPTAuth:
    """Authentication handler for ChatGPT accounts."""

    def supported_auth_methods(self) -> list[AuthMethod]:
        return [
            AuthMethod.SESSION_COOKIE,
            AuthMethod.MANUAL_TOKEN,
        ]

    async def authenticate(self, account: Account) -> Credential:
        """Try auth strategies in order."""
        errors: list[str] = []

        # Strategy 1: Check existing stored credential
        if account.credential and not account.credential.is_expired:
            secret = get_secret(account.credential.keyring_key)
            if secret:
                if await self._validate_session(secret):
                    return account.credential
                errors.append("Stored session is invalid")

        raise AuthenticationError(
            provider="chatgpt",
            message=(
                f"No valid credentials for '{account.label}'. "
                f"Tried: {', '.join(errors) if errors else 'no strategies available'}. "
                f"Use 'ai-usage accounts login {account.id}' to provide a session token."
            ),
            account_id=account.id,
        )

    async def authenticate_with_session_token(
        self, account: Account, access_token: str
    ) -> Credential:
        """Authenticate with a ChatGPT access token / session cookie."""
        if not await self._validate_session(access_token):
            raise AuthenticationError(
                provider="chatgpt",
                message="Invalid session token — could not access ChatGPT API",
                account_id=account.id,
            )

        keyring_key = f"chatgpt-session-{account.id}"
        store_secret(keyring_key, access_token)

        return Credential(
            auth_method=AuthMethod.SESSION_COOKIE,
            keyring_key=keyring_key,
        )

    async def refresh_credential(self, account: Account) -> Credential:
        """ChatGPT sessions can't be refreshed — re-auth needed."""
        raise AuthenticationError(
            provider="chatgpt",
            message="Session tokens can't be refreshed — provide a new token",
            account_id=account.id,
        )

    async def validate_credential(self, account: Account) -> bool:
        if not account.credential:
            return False
        secret = get_secret(account.credential.keyring_key)
        if not secret:
            return False
        return await self._validate_session(secret)

    async def _validate_session(self, token: str) -> bool:
        """Validate a ChatGPT session token."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{CHATGPT_BASE}/backend-api/me",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
