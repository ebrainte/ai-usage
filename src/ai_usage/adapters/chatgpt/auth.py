"""ChatGPT authentication adapter.

Supports three auth methods (matching the Codex CLI's own auth flow):
1. Browser PKCE OAuth — opens browser to auth.openai.com, localhost callback (default)
2. Device code flow — for headless/SSH environments (custom OpenAI endpoints)
3. Codex CLI import — reads existing tokens from ~/.codex/auth.json (fallback)
4. Manual session token — user pastes access token (last resort)

Auth flow reverse-engineered from the Codex CLI source (codex-rs/login/).
Tokens are stored in our OWN keyring namespace, independent of Codex CLI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from ai_usage.adapters.storage.file import get_secret, store_secret
from ai_usage.domain.exceptions import AuthenticationError
from ai_usage.domain.models import Account, AuthMethod, Credential

logger = logging.getLogger(__name__)

# --- Constants (from Codex CLI source: codex-rs/login/src/auth/manager.rs) ---
OPENAI_AUTH_BASE = "https://auth.openai.com"
OPENAI_TOKEN_URL = f"{OPENAI_AUTH_BASE}/oauth/token"
OPENAI_AUTHORIZE_URL = f"{OPENAI_AUTH_BASE}/oauth/authorize"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# Device flow endpoints (custom, NOT RFC 8628)
OPENAI_DEVICE_USERCODE_URL = f"{OPENAI_AUTH_BASE}/api/accounts/deviceauth/usercode"
OPENAI_DEVICE_TOKEN_URL = f"{OPENAI_AUTH_BASE}/api/accounts/deviceauth/token"
OPENAI_DEVICE_VERIFY_URL = f"{OPENAI_AUTH_BASE}/codex/device"
OPENAI_DEVICE_CALLBACK_URI = f"{OPENAI_AUTH_BASE}/deviceauth/callback"

# OAuth scopes (from codex-rs/login/src/auth/manager.rs)
OPENAI_SCOPES = "openid profile email offline_access api.connectors.read api.connectors.invoke"

# Browser PKCE callback — port MUST be 1455.
# OpenAI has http://localhost:1455/auth/callback whitelisted for this client ID.
# Using any other port causes "unknown_error" from auth.openai.com.
OAUTH_CALLBACK_PATH = "/auth/callback"
OAUTH_CALLBACK_PORT = 1455

# Token refresh trigger: >8 days since last refresh (matches Codex CLI)
REFRESH_STALENESS_DAYS = 8

# Device flow timeout: 15 minutes (matches Codex CLI)
DEVICE_FLOW_TIMEOUT = 900

# ChatGPT API for validation
CHATGPT_BASE = "https://chatgpt.com"

# Default Codex home directory
CODEX_HOME = Path.home() / ".codex"

# Non-retryable refresh errors
PERMANENT_REFRESH_ERRORS = {
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
}


def _resolve_codex_home() -> Path:
    """Resolve Codex home directory, respecting CODEX_HOME env var."""
    import os

    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home)
    return CODEX_HOME


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    Matches Codex CLI: 64 random bytes -> base64url no padding for verifier,
    SHA-256 -> base64url no padding for challenge.
    """
    verifier_bytes = secrets.token_bytes(64)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _generate_state() -> str:
    """Generate OAuth state parameter: 32 random bytes -> base64url no padding."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for the OpenAI OAuth callback redirect."""

    authorization_code: str | None = None
    received_state: str | None = None
    error: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != OAUTH_CALLBACK_PATH:
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


class ChatGPTAuth:
    """Authentication handler for ChatGPT accounts.

    Supports browser PKCE OAuth, device code flow, Codex CLI import,
    and manual session token — all storing credentials independently
    in our own keyring namespace.
    """

    def supported_auth_methods(self) -> list[AuthMethod]:
        return [
            AuthMethod.OAUTH_TOKEN,
            AuthMethod.DEVICE_FLOW,
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
                token_data = self._parse_stored_secret(secret)
                access_token = token_data.get("access_token", secret)
                if await self._validate_session(access_token):
                    return account.credential
                errors.append("Stored token is invalid")

                # Try refresh if we have a refresh token
                refresh_token = token_data.get("refresh_token")
                if refresh_token:
                    try:
                        return await self._refresh_and_store(account, token_data)
                    except AuthenticationError as e:
                        errors.append(f"Refresh failed: {e.message}")

        # Strategy 2: Try Codex CLI import
        codex_home = _resolve_codex_home()
        auth_file = codex_home / "auth.json"
        if auth_file.exists():
            try:
                return await self.authenticate_from_codex(account)
            except AuthenticationError as e:
                errors.append(f"Codex import: {e.message}")

        raise AuthenticationError(
            provider="chatgpt",
            message=(
                f"No valid credentials for '{account.label}'. "
                f"Tried: {', '.join(errors) if errors else 'no strategies available'}. "
                f"Use 'ai-usage accounts login {account.id} --browser' for OAuth login."
            ),
            account_id=account.id,
        )

    # --- Browser PKCE OAuth Flow ---

    async def authenticate_with_browser(
        self,
        account: Account,
        on_url: callable | None = None,
        timeout: int = 120,
    ) -> Credential:
        """Authenticate via OpenAI OAuth PKCE browser flow.

        Mirrors the Codex CLI's browser auth (codex-rs/login/src/server.rs):
        1. Generate PKCE verifier/challenge (64 bytes)
        2. Start local HTTP server for callback
        3. Open browser to auth.openai.com/oauth/authorize
        4. User logs in and authorizes
        5. Exchange authorization code for tokens (form-encoded)
        6. Store tokens independently in our keyring

        Args:
            account: The account to authenticate.
            on_url: Optional callback with the authorization URL.
            timeout: How long to wait for the callback (seconds).
        """
        code_verifier, code_challenge = _generate_pkce()
        state = _generate_state()

        # Start local server on port 1455 (must match OpenAI's whitelisted redirect URI)
        server = HTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), _OAuthCallbackHandler)
        port = server.server_address[1]
        redirect_uri = f"http://localhost:{port}{OAUTH_CALLBACK_PATH}"

        # Reset handler state
        _OAuthCallbackHandler.authorization_code = None
        _OAuthCallbackHandler.received_state = None
        _OAuthCallbackHandler.error = None

        # Build authorization URL (matching Codex CLI parameter order)
        params = {
            "response_type": "code",
            "client_id": OPENAI_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": OPENAI_SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "codex_cli_rs",
        }
        auth_url = f"{OPENAI_AUTHORIZE_URL}?{urlencode(params)}"

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
                    provider="chatgpt",
                    message=f"Authorization denied: {_OAuthCallbackHandler.error}",
                    account_id=account.id,
                )

            code = _OAuthCallbackHandler.authorization_code
            if not code:
                raise AuthenticationError(
                    provider="chatgpt",
                    message="Authorization timed out — no callback received",
                    account_id=account.id,
                )

            # Validate state
            if _OAuthCallbackHandler.received_state != state:
                raise AuthenticationError(
                    provider="chatgpt",
                    message="Invalid state parameter — possible CSRF attack",
                    account_id=account.id,
                )

            # Exchange code for tokens (form-encoded, NOT JSON — per Codex CLI source)
            tokens = await self._exchange_code(code, code_verifier, redirect_uri)

            # Extract email from id_token
            email = None
            if tokens.get("id_token"):
                email = self._extract_email_from_jwt(tokens["id_token"])

            # Extract account_id from id_token claims
            openai_account_id = None
            if tokens.get("id_token"):
                openai_account_id = self._extract_account_id_from_jwt(tokens["id_token"])

            # Store tokens in our keyring
            keyring_key = f"chatgpt-oauth-{account.id}"
            token_data = {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token"),
                "id_token": tokens.get("id_token"),
                "account_id": openai_account_id,
                "source": "browser_oauth",
                "last_refresh": datetime.now(timezone.utc).isoformat(),
            }
            store_secret(keyring_key, json.dumps(token_data))

            # Update email on the account if found
            if email:
                account.email = email

            logger.info("Browser OAuth login successful for %s", account.label)

            return Credential(
                auth_method=AuthMethod.OAUTH_TOKEN,
                keyring_key=keyring_key,
                extra={"openai_account_id": openai_account_id} if openai_account_id else {},
            )

        finally:
            server.shutdown()

    async def _exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> dict:
        """Exchange authorization code for tokens.

        IMPORTANT: Initial token exchange uses form-encoded body (NOT JSON).
        This matches the Codex CLI (codex-rs/login/src/server.rs).
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                OPENAI_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": OPENAI_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if resp.status_code != 200:
                error_text = resp.text[:200]
                try:
                    error_data = resp.json()
                    error_text = error_data.get("error_description", error_text)
                except Exception:
                    pass
                raise AuthenticationError(
                    provider="chatgpt",
                    message=f"Token exchange failed ({resp.status_code}): {error_text}",
                )

            data = resp.json()
            if "access_token" not in data:
                raise AuthenticationError(
                    provider="chatgpt",
                    message="Token exchange response missing access_token",
                )

            logger.info("OpenAI token exchange successful")
            return data

    # --- Device Code Flow ---

    async def authenticate_with_device_flow(
        self,
        account: Account,
        on_user_code: callable | None = None,
    ) -> Credential:
        """Authenticate via OpenAI device code flow.

        Custom flow (NOT RFC 8628) — uses OpenAI-specific endpoints:
        1. POST /api/accounts/deviceauth/usercode -> {device_auth_id, user_code}
        2. User enters code at auth.openai.com/codex/device
        3. Poll /api/accounts/deviceauth/token -> {authorization_code, code_verifier}
        4. Exchange code using standard token endpoint

        Args:
            account: The account to authenticate.
            on_user_code: Callback(verification_uri, user_code) for display.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            # Step 1: Request user code
            resp = await client.post(
                OPENAI_DEVICE_USERCODE_URL,
                json={"client_id": OPENAI_CLIENT_ID},
            )

            if resp.status_code == 404:
                raise AuthenticationError(
                    provider="chatgpt",
                    message="Device code flow not available — use browser OAuth instead",
                    account_id=account.id,
                )

            resp.raise_for_status()
            device_data = resp.json()

            device_auth_id = device_data["device_auth_id"]
            user_code = device_data["user_code"]
            interval = int(device_data.get("interval", "5"))

            # Notify the caller about the user code
            if on_user_code:
                on_user_code(OPENAI_DEVICE_VERIFY_URL, user_code)
            else:
                logger.info("Go to %s and enter code: %s", OPENAI_DEVICE_VERIFY_URL, user_code)

            # Step 2: Poll for authorization
            max_attempts = DEVICE_FLOW_TIMEOUT // interval
            for _ in range(max_attempts):
                await asyncio.sleep(interval)

                poll_resp = await client.post(
                    OPENAI_DEVICE_TOKEN_URL,
                    json={
                        "device_auth_id": device_auth_id,
                        "user_code": user_code,
                    },
                )

                if poll_resp.status_code == 200:
                    # Authorization complete — server provides the code + PKCE verifier
                    result = poll_resp.json()
                    authorization_code = result["authorization_code"]
                    code_verifier = result["code_verifier"]

                    # Step 3: Exchange code for tokens using device callback URI
                    tokens = await self._exchange_code_device(authorization_code, code_verifier)

                    # Extract email + account_id from id_token
                    email = None
                    openai_account_id = None
                    if tokens.get("id_token"):
                        email = self._extract_email_from_jwt(tokens["id_token"])
                        openai_account_id = self._extract_account_id_from_jwt(tokens["id_token"])

                    # Store tokens
                    keyring_key = f"chatgpt-oauth-{account.id}"
                    token_data = {
                        "access_token": tokens["access_token"],
                        "refresh_token": tokens.get("refresh_token"),
                        "id_token": tokens.get("id_token"),
                        "account_id": openai_account_id,
                        "source": "device_flow",
                        "last_refresh": datetime.now(timezone.utc).isoformat(),
                    }
                    store_secret(keyring_key, json.dumps(token_data))

                    if email:
                        account.email = email

                    logger.info("Device flow auth completed for %s", account.label)

                    return Credential(
                        auth_method=AuthMethod.DEVICE_FLOW,
                        keyring_key=keyring_key,
                        extra=(
                            {"openai_account_id": openai_account_id} if openai_account_id else {}
                        ),
                    )

                elif poll_resp.status_code in (403, 404):
                    # Not yet authorized — keep polling
                    continue
                else:
                    raise AuthenticationError(
                        provider="chatgpt",
                        message=f"Device flow poll error ({poll_resp.status_code}): {poll_resp.text[:200]}",
                        account_id=account.id,
                    )

            raise AuthenticationError(
                provider="chatgpt",
                message="Device flow timed out — user did not authorize in time",
                account_id=account.id,
            )

    async def _exchange_code_device(self, code: str, code_verifier: str) -> dict:
        """Exchange device flow authorization code for tokens.

        Uses the special device callback redirect_uri.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                OPENAI_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": OPENAI_DEVICE_CALLBACK_URI,
                    "client_id": OPENAI_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if resp.status_code != 200:
                error_text = resp.text[:200]
                try:
                    error_data = resp.json()
                    error_text = error_data.get("error_description", error_text)
                except Exception:
                    pass
                raise AuthenticationError(
                    provider="chatgpt",
                    message=f"Device flow token exchange failed ({resp.status_code}): {error_text}",
                )

            data = resp.json()
            if "access_token" not in data:
                raise AuthenticationError(
                    provider="chatgpt",
                    message="Device flow token exchange response missing access_token",
                )

            return data

    # --- Codex CLI Import ---

    async def authenticate_from_codex(self, account: Account) -> Credential:
        """Import OAuth tokens from Codex CLI's auth.json."""
        codex_home = _resolve_codex_home()
        auth_file = codex_home / "auth.json"

        if not auth_file.exists():
            raise AuthenticationError(
                provider="chatgpt",
                message=f"Codex auth file not found at {auth_file}. Use --browser for OAuth login instead.",
                account_id=account.id,
            )

        try:
            data = json.loads(auth_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            raise AuthenticationError(
                provider="chatgpt",
                message=f"Failed to read Codex auth file: {e}",
                account_id=account.id,
            )

        tokens = data.get("tokens", {})
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        id_token = tokens.get("id_token")
        account_id_openai = tokens.get("account_id")
        last_refresh = data.get("last_refresh")

        if not access_token:
            raise AuthenticationError(
                provider="chatgpt",
                message="No access_token found in Codex auth.json. Run 'codex' to login first.",
                account_id=account.id,
            )

        # Check if token needs refresh (>8 days since last refresh)
        needs_refresh = True
        if last_refresh:
            try:
                last_dt = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - last_dt).days
                needs_refresh = age_days > REFRESH_STALENESS_DAYS
            except (ValueError, TypeError):
                needs_refresh = True

        if needs_refresh and refresh_token:
            logger.info("Codex token is stale (%s), refreshing...", last_refresh)
            try:
                new_tokens = await self._refresh_token(refresh_token)
                access_token = new_tokens["access_token"]
                refresh_token = new_tokens.get("refresh_token", refresh_token)
                if new_tokens.get("id_token"):
                    id_token = new_tokens["id_token"]
            except AuthenticationError:
                logger.warning("Token refresh failed, trying existing access token")

        # Validate the access token
        if not await self._validate_session(access_token):
            if refresh_token:
                try:
                    new_tokens = await self._refresh_token(refresh_token)
                    access_token = new_tokens["access_token"]
                    refresh_token = new_tokens.get("refresh_token", refresh_token)
                    if new_tokens.get("id_token"):
                        id_token = new_tokens["id_token"]
                except AuthenticationError:
                    raise AuthenticationError(
                        provider="chatgpt",
                        message="Codex token is invalid and refresh failed. Use --browser for OAuth login.",
                        account_id=account.id,
                    )
            else:
                raise AuthenticationError(
                    provider="chatgpt",
                    message="Codex access token is invalid. Use --browser for OAuth login.",
                    account_id=account.id,
                )

        # Extract email from id_token JWT (if available)
        email = None
        if id_token:
            email = self._extract_email_from_jwt(id_token)

        # Store in our own keyring
        keyring_key = f"chatgpt-oauth-{account.id}"
        token_data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "account_id": account_id_openai,
            "source": "codex_import",
            "last_refresh": datetime.now(timezone.utc).isoformat(),
        }
        store_secret(keyring_key, json.dumps(token_data))

        if email:
            account.email = email

        return Credential(
            auth_method=AuthMethod.OAUTH_TOKEN,
            keyring_key=keyring_key,
            extra={"openai_account_id": account_id_openai} if account_id_openai else {},
        )

    # --- Session Token (manual fallback) ---

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

    # --- Token Refresh ---

    async def refresh_credential(self, account: Account) -> Credential:
        """Refresh an OAuth token credential."""
        if not account.credential:
            raise AuthenticationError(
                provider="chatgpt",
                message="No credential to refresh",
                account_id=account.id,
            )

        secret = get_secret(account.credential.keyring_key)
        if not secret:
            raise AuthenticationError(
                provider="chatgpt",
                message="Credential not found in keyring",
                account_id=account.id,
            )

        token_data = self._parse_stored_secret(secret)
        refresh_token = token_data.get("refresh_token")

        if not refresh_token:
            raise AuthenticationError(
                provider="chatgpt",
                message="No refresh token — re-authenticate with --browser or --device-flow",
                account_id=account.id,
            )

        return await self._refresh_and_store(account, token_data)

    # --- Validation ---

    async def validate_credential(self, account: Account) -> bool:
        if not account.credential:
            return False
        secret = get_secret(account.credential.keyring_key)
        if not secret:
            return False
        token_data = self._parse_stored_secret(secret)
        access_token = token_data.get("access_token", secret)
        return await self._validate_session(access_token)

    # --- Internal helpers ---

    async def _refresh_and_store(self, account: Account, token_data: dict) -> Credential:
        """Refresh token and update keyring."""
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise AuthenticationError(
                provider="chatgpt",
                message="No refresh token available",
                account_id=account.id,
            )

        new_tokens = await self._refresh_token(refresh_token)

        # Update stored token data (preserve existing fields, update tokens)
        token_data["access_token"] = new_tokens["access_token"]
        if new_tokens.get("refresh_token"):
            token_data["refresh_token"] = new_tokens["refresh_token"]
        if new_tokens.get("id_token"):
            token_data["id_token"] = new_tokens["id_token"]
        token_data["last_refresh"] = datetime.now(timezone.utc).isoformat()

        keyring_key = (
            account.credential.keyring_key if account.credential else f"chatgpt-oauth-{account.id}"
        )
        store_secret(keyring_key, json.dumps(token_data))

        return Credential(
            auth_method=AuthMethod.OAUTH_TOKEN,
            keyring_key=keyring_key,
            extra=token_data.get("extra", {}),
        )

    async def _refresh_token(self, refresh_token: str) -> dict:
        """Refresh an OAuth token via auth.openai.com.

        IMPORTANT: Token refresh uses JSON body (NOT form-encoded).
        This is different from the initial token exchange which uses form-encoded.
        Matches Codex CLI behavior.
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    OPENAI_TOKEN_URL,
                    json={
                        "client_id": OPENAI_CLIENT_ID,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    headers={"Content-Type": "application/json"},
                )

                if resp.status_code == 401:
                    body = {}
                    try:
                        body = resp.json()
                    except Exception:
                        pass
                    error_code = body.get("error", "")
                    if error_code in PERMANENT_REFRESH_ERRORS:
                        raise AuthenticationError(
                            provider="chatgpt",
                            message=(
                                f"Refresh token {error_code.replace('_', ' ')} "
                                f"— re-authenticate with --browser or --device-flow"
                            ),
                        )
                    raise AuthenticationError(
                        provider="chatgpt",
                        message="Token refresh unauthorized",
                    )

                resp.raise_for_status()
                data = resp.json()

                if "access_token" not in data:
                    raise AuthenticationError(
                        provider="chatgpt",
                        message="Token refresh response missing access_token",
                    )

                logger.info("Successfully refreshed ChatGPT OAuth token")
                return data

        except httpx.HTTPError as e:
            raise AuthenticationError(
                provider="chatgpt",
                message=f"Token refresh HTTP error: {e}",
            )

    async def _validate_session(self, token: str) -> bool:
        """Validate a ChatGPT access token."""
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

    def _parse_stored_secret(self, secret: str) -> dict:
        """Parse a stored secret — could be JSON (OAuth) or plain string (session token)."""
        try:
            return json.loads(secret)
        except (json.JSONDecodeError, TypeError):
            return {"access_token": secret}

    def _extract_email_from_jwt(self, id_token: str) -> str | None:
        """Extract email from an OpenAI id_token JWT (no verification)."""
        try:
            parts = id_token.split(".")
            if len(parts) < 2:
                return None
            payload = parts[1]
            # Add padding
            payload += "=" * (4 - len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload)
            claims = json.loads(decoded)
            return claims.get("email") or claims.get("https://api.openai.com/profile.email")
        except Exception:
            return None

    def _extract_account_id_from_jwt(self, id_token: str) -> str | None:
        """Extract OpenAI account_id from id_token JWT claims."""
        try:
            parts = id_token.split(".")
            if len(parts) < 2:
                return None
            payload = parts[1]
            payload += "=" * (4 - len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload)
            claims = json.loads(decoded)
            # Check nested auth claims
            auth_claims = claims.get("https://api.openai.com/auth", {})
            return auth_claims.get("chatgpt_account_id")
        except Exception:
            return None
