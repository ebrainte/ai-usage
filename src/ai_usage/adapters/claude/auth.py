"""Claude authentication adapter.

Supports multiple auth strategies in priority order:
1. OAuth PKCE flow (independent browser-based login)
2. OAuth token import from Claude Code CLI credentials (one-time)
3. Manual session key (user pastes their sessionKey)

The app stores credentials in its OWN keyring namespace, independent of
whatever CLI tools are logged in on the machine. Token refresh is handled
independently via the platform.claude.com OAuth endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import subprocess
import base64
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from urllib.parse import urlparse, parse_qs, urlencode
import webbrowser

import httpx

from ai_usage.adapters.storage.file import get_secret, store_secret
from ai_usage.domain.exceptions import AuthenticationError
from ai_usage.domain.models import Account, AuthMethod, Credential

logger = logging.getLogger(__name__)

CLAUDE_API_BASE = "https://claude.ai"
ANTHROPIC_API_BASE = "https://api.anthropic.com"

# OAuth configuration (from Claude Code v2.1.88 binary)
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_AUTHORIZE_URL_CLAUDE_AI = "https://claude.com/cai/oauth/authorize"
OAUTH_AUTHORIZE_URL_CONSOLE = "https://platform.claude.com/oauth/authorize"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REDIRECT_PATH = "/callback"
OAUTH_SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)

# Non-retryable token refresh errors
PERMANENT_REFRESH_ERRORS = {"invalid_refresh_token", "expired_refresh_token", "token_expired"}


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(32)  # 43 chars
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for the OAuth callback redirect."""

    authorization_code: str | None = None
    received_state: str | None = None
    error: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != OAUTH_REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]

        if error:
            _OAuthCallbackHandler.error = error
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authorization failed</h2>"
                b"<p>You can close this tab.</p></body></html>"
            )
        elif code:
            _OAuthCallbackHandler.authorization_code = code
            _OAuthCallbackHandler.received_state = state
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login successful!</h2>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Missing authorization code</h2></body></html>")

    def log_message(self, format, *args):
        # Suppress HTTP server log noise
        pass


class ClaudeAuth:
    """Authentication handler for Claude accounts."""

    def supported_auth_methods(self) -> list[AuthMethod]:
        return [
            AuthMethod.OAUTH_TOKEN,
            AuthMethod.SESSION_COOKIE,
            AuthMethod.MANUAL_TOKEN,
        ]

    async def authenticate(self, account: Account) -> Credential:
        """Try auth strategies in order until one works."""
        errors: list[str] = []

        # Strategy 1: Check if we already have a stored credential
        if account.credential:
            secret = get_secret(account.credential.keyring_key)
            if secret:
                # Try to refresh if we have a refresh token
                if account.credential.auth_method == AuthMethod.OAUTH_TOKEN:
                    refreshed = await self._try_refresh_stored(account, secret)
                    if refreshed:
                        return refreshed
                # Validate it still works
                if await self._validate_oauth_token(secret):
                    return account.credential
                if await self._validate_session_key(secret):
                    return account.credential
                errors.append("Stored credential is invalid or expired")

        # Strategy 2: Import from Claude Code CLI (one-time import)
        try:
            cred = await self._import_from_claude_cli(account)
            if cred:
                return cred
        except Exception as e:
            errors.append(f"Claude CLI import: {e}")

        # Strategy 3: If we have nothing, need manual login
        raise AuthenticationError(
            provider="claude",
            message=(
                f"No valid credentials found for '{account.label}'. "
                f"Tried: {', '.join(errors) if errors else 'no strategies available'}. "
                f"Use 'ai-usage accounts login {account.id}' to authenticate."
            ),
            account_id=account.id,
        )

    async def authenticate_with_browser(
        self,
        account: Account,
        on_url: callable | None = None,
        timeout: int = 120,
    ) -> Credential:
        """Authenticate via OAuth PKCE browser flow.

        1. Start local HTTP server for callback
        2. Open browser to Claude authorization URL
        3. User logs in and authorizes
        4. Exchange authorization code for tokens
        5. Store tokens independently

        Args:
            account: The account to authenticate.
            on_url: Optional callback with the authorization URL (for display).
            timeout: How long to wait for the callback (seconds).
        """
        verifier, challenge = _generate_pkce()
        state = secrets.token_urlsafe(32)

        # Start local server on random port
        server = HTTPServer(("localhost", 0), _OAuthCallbackHandler)
        port = server.server_address[1]
        redirect_uri = f"http://localhost:{port}{OAUTH_REDIRECT_PATH}"

        # Reset handler state
        _OAuthCallbackHandler.authorization_code = None
        _OAuthCallbackHandler.received_state = None
        _OAuthCallbackHandler.error = None

        # Build authorization URL
        # Matches Claude Code's URL construction: JS URL class + searchParams.append()
        # which produces application/x-www-form-urlencoded style (spaces as +).
        # Parameters are appended in the same order as Claude Code.
        params = [
            ("code", "true"),
            ("client_id", OAUTH_CLIENT_ID),
            ("response_type", "code"),
            ("redirect_uri", redirect_uri),
            ("scope", OAUTH_SCOPES),
            ("code_challenge", challenge),
            ("code_challenge_method", "S256"),
            ("state", state),
        ]
        auth_url = f"{OAUTH_AUTHORIZE_URL_CLAUDE_AI}?{urlencode(params)}"

        # Start server in background thread
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            # Open browser / notify caller
            if on_url:
                on_url(auth_url)
            webbrowser.open(auth_url)

            # Wait for callback
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                if _OAuthCallbackHandler.authorization_code or _OAuthCallbackHandler.error:
                    break
                await asyncio.sleep(0.5)

            if _OAuthCallbackHandler.error:
                raise AuthenticationError(
                    provider="claude",
                    message=f"Authorization denied: {_OAuthCallbackHandler.error}",
                    account_id=account.id,
                )

            code = _OAuthCallbackHandler.authorization_code
            if not code:
                raise AuthenticationError(
                    provider="claude",
                    message="Authorization timed out — no callback received",
                    account_id=account.id,
                )

            # Validate state
            if _OAuthCallbackHandler.received_state != state:
                raise AuthenticationError(
                    provider="claude",
                    message="Invalid state parameter — possible CSRF attack",
                    account_id=account.id,
                )

            # Exchange code for tokens
            tokens = await self._exchange_code(code, verifier, redirect_uri, state)

            # Store tokens
            keyring_key = f"claude-oauth-{account.id}"
            store_secret(keyring_key, json.dumps(tokens))

            logger.info("Browser OAuth login successful for %s", account.label)

            expires_at = None
            if tokens.get("expires_at"):
                expires_at = datetime.fromtimestamp(tokens["expires_at"] / 1000, tz=timezone.utc)

            return Credential(
                auth_method=AuthMethod.OAUTH_TOKEN,
                keyring_key=keyring_key,
                expires_at=expires_at,
                extra={
                    "scopes": tokens.get("scopes", []),
                    "subscription_type": tokens.get("subscription_type"),
                },
            )

        finally:
            server.shutdown()

    async def _exchange_code(self, code: str, verifier: str, redirect_uri: str, state: str) -> dict:
        """Exchange authorization code for tokens."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                OAUTH_TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": OAUTH_CLIENT_ID,
                    "code_verifier": verifier,
                    "state": state,
                },
                headers={"Content-Type": "application/json"},
            )

            if resp.status_code != 200:
                error_data = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
                error_msg = error_data.get("error", {}).get("message", resp.text[:200])
                raise AuthenticationError(
                    provider="claude",
                    message=f"Token exchange failed ({resp.status_code}): {error_msg}",
                )

            data = resp.json()
            # Build our stored token format
            scopes = data.get("scope", "").split() if data.get("scope") else []
            return {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "expires_at": (
                    int((datetime.now(timezone.utc).timestamp() + data["expires_in"]) * 1000)
                    if data.get("expires_in")
                    else None
                ),
                "scopes": scopes,
                "subscription_type": data.get("subscription_type"),
            }

    async def authenticate_with_session_key(self, account: Account, session_key: str) -> Credential:
        """Authenticate using a manually-provided session key."""
        if not await self._validate_session_key(session_key):
            raise AuthenticationError(
                provider="claude",
                message="Invalid session key — could not access Claude API",
                account_id=account.id,
            )

        keyring_key = f"claude-session-{account.id}"
        store_secret(keyring_key, session_key)

        return Credential(
            auth_method=AuthMethod.SESSION_COOKIE,
            keyring_key=keyring_key,
        )

    async def authenticate_with_oauth_token(
        self, account: Account, access_token: str, refresh_token: str | None = None
    ) -> Credential:
        """Authenticate using an OAuth access token."""
        if not await self._validate_oauth_token(access_token):
            raise AuthenticationError(
                provider="claude",
                message="Invalid OAuth token",
                account_id=account.id,
            )

        keyring_key = f"claude-oauth-{account.id}"
        token_data = {"access_token": access_token}
        if refresh_token:
            token_data["refresh_token"] = refresh_token
        store_secret(keyring_key, json.dumps(token_data))

        return Credential(
            auth_method=AuthMethod.OAUTH_TOKEN,
            keyring_key=keyring_key,
        )

    async def refresh_credential(self, account: Account) -> Credential:
        """Try to refresh an expired OAuth token."""
        if not account.credential:
            raise AuthenticationError(
                provider="claude",
                message="No credential to refresh",
                account_id=account.id,
            )

        secret = get_secret(account.credential.keyring_key)
        if not secret:
            raise AuthenticationError(
                provider="claude",
                message="Credential not found in keyring",
                account_id=account.id,
            )

        if account.credential.auth_method == AuthMethod.OAUTH_TOKEN:
            refreshed = await self._try_refresh_stored(account, secret)
            if refreshed:
                return refreshed

        raise AuthenticationError(
            provider="claude",
            message="Cannot refresh credential — re-authentication required. "
            "Use 'ai-usage accounts login --browser' for independent OAuth.",
            account_id=account.id,
        )

    async def _try_refresh_stored(self, account: Account, secret: str) -> Credential | None:
        """Attempt to refresh using stored token data."""
        try:
            token_data = json.loads(secret)
            refresh_token = token_data.get("refresh_token")
            scopes = token_data.get("scopes", [])
            if refresh_token:
                new_cred = await self._refresh_oauth_token(account, refresh_token, scopes)
                if new_cred:
                    return new_cred
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    async def validate_credential(self, account: Account) -> bool:
        """Check if the current credential is still valid."""
        if not account.credential:
            return False

        secret = get_secret(account.credential.keyring_key)
        if not secret:
            return False

        if account.credential.auth_method == AuthMethod.OAUTH_TOKEN:
            token = self._extract_oauth_access_token(secret)
            return await self._validate_oauth_token(token) if token else False
        elif account.credential.auth_method == AuthMethod.SESSION_COOKIE:
            return await self._validate_session_key(secret)

        return False

    # --- Private helpers ---

    async def _import_from_claude_cli(self, account: Account) -> Credential | None:
        """Import OAuth credentials from Claude Code CLI (one-time).

        Reads from macOS Keychain entry 'Claude Code-credentials'.
        This is a ONE-TIME import — after this, we store in our own keyring
        and handle refresh independently.
        """
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return None

            raw = result.stdout.strip()
            data = json.loads(raw)

            # Claude Code stores: {"claudeAiOauth": {"accessToken": "...", "refreshToken": "...", ...}}
            oauth_data = data.get("claudeAiOauth", {})
            access_token = oauth_data.get("accessToken")
            refresh_token = oauth_data.get("refreshToken")
            scopes = oauth_data.get("scopes", [])
            expires_at = oauth_data.get("expiresAt")
            subscription_type = oauth_data.get("subscriptionType")

            if not access_token:
                return None

            # Store in OUR keyring with full token data
            keyring_key = f"claude-oauth-{account.id}"
            token_data = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "scopes": scopes,
                "expires_at": expires_at,
                "subscription_type": subscription_type,
            }
            store_secret(keyring_key, json.dumps(token_data))

            logger.info("Imported Claude CLI OAuth credentials for %s", account.label)

            cred_expires_at = None
            if expires_at:
                cred_expires_at = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)

            return Credential(
                auth_method=AuthMethod.OAUTH_TOKEN,
                keyring_key=keyring_key,
                expires_at=cred_expires_at,
                extra={
                    "scopes": scopes,
                    "subscription_type": subscription_type,
                },
            )

        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to import Claude CLI credentials: %s", e)
            return None

    async def _validate_oauth_token(self, token: str) -> bool:
        """Validate an OAuth token against the Anthropic API.

        Uses api.anthropic.com (not claude.ai which is behind Cloudflare).
        """
        actual_token = self._extract_oauth_access_token(token) or token
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{ANTHROPIC_API_BASE}/api/oauth/usage",
                    headers={
                        "Authorization": f"Bearer {actual_token}",
                        "anthropic-beta": "oauth-2025-04-20",
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def _validate_session_key(self, session_key: str) -> bool:
        """Validate a session key cookie.

        Note: claude.ai is behind Cloudflare, so this may fail from non-browser
        clients. Session key auth is less reliable than OAuth.
        """
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(
                    f"{CLAUDE_API_BASE}/api/account",
                    headers={"Cookie": f"sessionKey={session_key}"},
                    timeout=10,
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def _refresh_oauth_token(
        self, account: Account, refresh_token: str, scopes: list[str] | None = None
    ) -> Credential | None:
        """Refresh an OAuth token and store the new one."""
        refreshed = await self._do_oauth_refresh(refresh_token, scopes)
        if not refreshed:
            return None

        keyring_key = f"claude-oauth-{account.id}"
        token_data = {
            "access_token": refreshed["access_token"],
            "refresh_token": refreshed.get("refresh_token", refresh_token),
            "scopes": refreshed.get("scopes", scopes or []),
            "expires_at": refreshed.get("expires_at"),
            "subscription_type": refreshed.get("subscription_type"),
        }
        store_secret(keyring_key, json.dumps(token_data))

        expires_at = None
        if refreshed.get("expires_at"):
            expires_at = datetime.fromtimestamp(refreshed["expires_at"] / 1000, tz=timezone.utc)

        return Credential(
            auth_method=AuthMethod.OAUTH_TOKEN,
            keyring_key=keyring_key,
            expires_at=expires_at,
            extra={
                "scopes": refreshed.get("scopes", scopes or []),
                "subscription_type": refreshed.get("subscription_type"),
            },
        )

    async def _do_oauth_refresh(
        self, refresh_token: str, scopes: list[str] | None = None
    ) -> dict | None:
        """Perform the actual OAuth token refresh.

        Uses platform.claude.com/v1/oauth/token with JSON body.
        This is the same endpoint Claude Code uses internally.
        """
        body: dict = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
        if scopes:
            body["scope"] = " ".join(scopes)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    OAUTH_TOKEN_URL,
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=15,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    new_scopes = (
                        data.get("scope", "").split() if data.get("scope") else scopes or []
                    )
                    return {
                        "access_token": data["access_token"],
                        "refresh_token": data.get("refresh_token"),
                        "expires_at": (
                            int(
                                (datetime.now(timezone.utc).timestamp() + data["expires_in"]) * 1000
                            )
                            if data.get("expires_in")
                            else None
                        ),
                        "scopes": new_scopes,
                        "subscription_type": data.get("subscription_type"),
                    }

                if resp.status_code == 429:
                    logger.warning("Rate limited during token refresh, will retry later")
                    return None

                # Check for permanent errors
                try:
                    error_data = resp.json()
                    error_type = error_data.get("error", {}).get("type", "")
                    error_msg = error_data.get("error", {}).get("message", "")
                    if error_type in PERMANENT_REFRESH_ERRORS:
                        logger.error("Permanent refresh error: %s — %s", error_type, error_msg)
                    else:
                        logger.warning(
                            "Token refresh failed (%d): %s — %s",
                            resp.status_code,
                            error_type,
                            error_msg,
                        )
                except Exception:
                    logger.warning(
                        "Token refresh failed (%d): %s",
                        resp.status_code,
                        resp.text[:200],
                    )

        except httpx.HTTPError as e:
            logger.debug("OAuth refresh HTTP error: %s", e)
        return None

    def _extract_oauth_access_token(self, secret: str) -> str | None:
        """Extract the access token from a stored OAuth secret."""
        try:
            data = json.loads(secret)
            return data.get("access_token")
        except (json.JSONDecodeError, AttributeError):
            # Might be a plain token string
            return secret if secret.startswith("sk-ant-") else None
