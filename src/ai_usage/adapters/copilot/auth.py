"""GitHub Copilot authentication adapter.

Supports:
1. GitHub device flow OAuth (recommended — standard flow, user approves in browser)
2. Manual personal access token (PAT)
3. Import from existing Copilot CLI keychain entry (one-time)

The app stores credentials in its OWN keyring namespace.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from ai_usage.adapters.storage.file import get_secret, store_secret
from ai_usage.domain.exceptions import AuthenticationError
from ai_usage.domain.models import Account, AuthMethod, Credential

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
# GitHub OAuth device flow endpoints
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

# Client ID for VS Code Copilot (used by CodexBar and other tools)
# This is a publicly known client ID used for Copilot device flow
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"


class CopilotAuth:
    """Authentication handler for GitHub Copilot."""

    def supported_auth_methods(self) -> list[AuthMethod]:
        return [
            AuthMethod.DEVICE_FLOW,
            AuthMethod.MANUAL_TOKEN,
        ]

    async def authenticate(self, account: Account) -> Credential:
        """Try auth strategies in order."""
        errors: list[str] = []

        # Strategy 1: Check existing stored credential
        if account.credential and not account.credential.is_expired:
            secret = get_secret(account.credential.keyring_key)
            if secret:
                if await self._validate_token(secret):
                    return account.credential
                errors.append("Stored token is invalid")

        # Strategy 2: Import from Copilot CLI keychain
        try:
            cred = await self._import_from_copilot_cli(account)
            if cred:
                return cred
        except Exception as e:
            errors.append(f"Copilot CLI import: {e}")

        raise AuthenticationError(
            provider="copilot",
            message=(
                f"No valid credentials for '{account.label}'. "
                f"Tried: {', '.join(errors) if errors else 'no strategies available'}. "
                f"Use 'ai-usage accounts login {account.id}' to authenticate via GitHub device flow."
            ),
            account_id=account.id,
        )

    async def authenticate_with_device_flow(
        self,
        account: Account,
        on_user_code: callable | None = None,
    ) -> Credential:
        """Perform GitHub device flow OAuth.

        Args:
            account: The account to authenticate.
            on_user_code: Callback(verification_uri, user_code) — called when
                         the user needs to enter the code in their browser.

        Returns:
            Credential with the GitHub OAuth token stored in keyring.
        """
        async with httpx.AsyncClient() as client:
            # Step 1: Request device code
            resp = await client.post(
                GITHUB_DEVICE_CODE_URL,
                data={
                    "client_id": COPILOT_CLIENT_ID,
                    "scope": "read:user",
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            device_data = resp.json()

            device_code = device_data["device_code"]
            user_code = device_data["user_code"]
            verification_uri = device_data["verification_uri"]
            interval = device_data.get("interval", 5)
            expires_in = device_data.get("expires_in", 900)

            # Notify the caller about the user code
            if on_user_code:
                on_user_code(verification_uri, user_code)
            else:
                logger.info("Go to %s and enter code: %s", verification_uri, user_code)

            # Step 2: Poll for token
            max_attempts = expires_in // interval
            for _ in range(max_attempts):
                await asyncio.sleep(interval)
                token_resp = await client.post(
                    GITHUB_TOKEN_URL,
                    data={
                        "client_id": COPILOT_CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
                token_data = token_resp.json()

                if "access_token" in token_data:
                    token = token_data["access_token"]
                    keyring_key = f"copilot-github-{account.id}"
                    store_secret(keyring_key, token)

                    logger.info("GitHub device flow auth completed for %s", account.label)

                    return Credential(
                        auth_method=AuthMethod.DEVICE_FLOW,
                        keyring_key=keyring_key,
                    )

                error = token_data.get("error")
                if error == "authorization_pending":
                    continue
                elif error == "slow_down":
                    interval += 5
                    continue
                elif error == "expired_token":
                    raise AuthenticationError(
                        provider="copilot",
                        message="Device flow expired — user did not authorize in time",
                        account_id=account.id,
                    )
                elif error == "access_denied":
                    raise AuthenticationError(
                        provider="copilot",
                        message="User denied the authorization request",
                        account_id=account.id,
                    )
                else:
                    raise AuthenticationError(
                        provider="copilot",
                        message=f"Unexpected device flow error: {error}",
                        account_id=account.id,
                    )

        raise AuthenticationError(
            provider="copilot",
            message="Device flow timed out",
            account_id=account.id,
        )

    async def authenticate_with_token(self, account: Account, token: str) -> Credential:
        """Authenticate with a manually-provided GitHub PAT or OAuth token."""
        if not await self._validate_token(token):
            raise AuthenticationError(
                provider="copilot",
                message="Invalid GitHub token",
                account_id=account.id,
            )

        keyring_key = f"copilot-github-{account.id}"
        store_secret(keyring_key, token)

        return Credential(
            auth_method=AuthMethod.MANUAL_TOKEN,
            keyring_key=keyring_key,
        )

    async def refresh_credential(self, account: Account) -> Credential:
        """GitHub tokens don't typically refresh — re-auth needed."""
        raise AuthenticationError(
            provider="copilot",
            message="GitHub tokens don't support refresh — re-authenticate via device flow",
            account_id=account.id,
        )

    async def validate_credential(self, account: Account) -> bool:
        if not account.credential:
            return False
        secret = get_secret(account.credential.keyring_key)
        if not secret:
            return False
        return await self._validate_token(secret)

    # --- Private helpers ---

    async def _import_from_copilot_cli(self, account: Account) -> Credential | None:
        """Import token from the copilot-cli keychain entry.

        Note: On macOS, the first access may trigger a Keychain Access
        permission dialog. The user needs to click 'Allow' or 'Always Allow'.
        """
        import subprocess

        try:
            # Try the copilot-cli keychain entry
            # Timeout of 15s to allow for Keychain permission dialog
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "copilot-cli",
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return None

            token = result.stdout.strip()
            if not token:
                return None

            if not await self._validate_token(token):
                return None

            # Store in OUR keyring
            keyring_key = f"copilot-github-{account.id}"
            store_secret(keyring_key, token)

            logger.info("Imported Copilot CLI token for %s", account.label)

            return Credential(
                auth_method=AuthMethod.DEVICE_FLOW,
                keyring_key=keyring_key,
            )

        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("Failed to import Copilot CLI credentials: %s", e)
            return None

    async def _validate_token(self, token: str) -> bool:
        """Validate a GitHub token."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GITHUB_API_BASE}/user",
                    headers={
                        "Authorization": f"token {token}",
                        "Accept": "application/json",
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
