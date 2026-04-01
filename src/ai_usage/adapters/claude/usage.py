"""Claude usage data fetcher.

Fetches usage data from the Anthropic API (OAuth endpoint):
- GET /api/oauth/usage — session/weekly usage data

For session cookie auth, falls back to claude.ai internal APIs:
- /api/organizations — get org UUID
- /api/organizations/{orgId}/usage — session/weekly usage
- /api/account — email + plan

Supports both OAuth tokens and session cookies.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from ai_usage.adapters.storage.file import get_secret
from ai_usage.domain.exceptions import AuthenticationError, FetchError
from ai_usage.domain.models import (
    Account,
    AuthMethod,
    ModelBreakdown,
    Provider,
    Quota,
    UsageData,
)

logger = logging.getLogger(__name__)

CLAUDE_API_BASE = "https://claude.ai"
ANTHROPIC_API_BASE = "https://api.anthropic.com"


class ClaudeUsage:
    """Fetches Claude usage data."""

    def supports_provider(self) -> str:
        return Provider.CLAUDE

    async def fetch_usage(self, account: Account) -> UsageData:
        """Fetch complete usage data for a Claude account.

        For OAuth tokens: uses api.anthropic.com/api/oauth/usage
        (avoids Cloudflare on claude.ai).

        If the stored token is expired, tries to re-read a fresh token
        from Claude Code's keychain (Claude Code refreshes its own tokens).
        """
        if not account.credential:
            return UsageData(
                account_id=account.id,
                provider=Provider.CLAUDE,
                error="No credentials configured",
            )

        secret = get_secret(account.credential.keyring_key)
        if not secret:
            return UsageData(
                account_id=account.id,
                provider=Provider.CLAUDE,
                error="Credentials not found in keyring",
            )

        if account.credential.auth_method == AuthMethod.OAUTH_TOKEN:
            return await self._fetch_usage_oauth(account, secret)
        else:
            # Session cookie — use claude.ai (may fail due to Cloudflare)
            headers = self._build_headers(account.credential.auth_method, secret)
            return await self._fetch_usage_cookie(account, headers)

    async def _fetch_usage_oauth(self, account: Account, secret: str) -> UsageData:
        """Fetch usage via Anthropic API OAuth endpoint.

        If the token is expired, tries to refresh it independently using
        our stored refresh_token via platform.claude.com OAuth endpoint.
        """
        token = self._extract_access_token(secret)
        if not token:
            return UsageData(
                account_id=account.id,
                provider=Provider.CLAUDE,
                error="Could not extract access token",
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{ANTHROPIC_API_BASE}/api/oauth/usage",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-beta": "oauth-2025-04-20",
                    },
                )

                if resp.status_code == 401:
                    # Token expired — try independent refresh
                    logger.info("OAuth token expired, attempting independent refresh")
                    fresh_token = await self._refresh_and_retry(account, secret)
                    if fresh_token:
                        resp = await client.get(
                            f"{ANTHROPIC_API_BASE}/api/oauth/usage",
                            headers={
                                "Authorization": f"Bearer {fresh_token}",
                                "anthropic-beta": "oauth-2025-04-20",
                            },
                        )
                    if resp.status_code == 401:
                        return UsageData(
                            account_id=account.id,
                            provider=Provider.CLAUDE,
                            error="Token expired — run 'ai-usage accounts login claude-personal --browser' to re-authenticate",
                        )

                if resp.status_code == 429:
                    return UsageData(
                        account_id=account.id,
                        provider=Provider.CLAUDE,
                        error="Rate limited — try again in a few minutes",
                    )

                if resp.status_code != 200:
                    # Provide a friendly error instead of raw API response
                    error_msg = self._parse_api_error(resp)
                    return UsageData(
                        account_id=account.id,
                        provider=Provider.CLAUDE,
                        error=error_msg,
                    )

                usage_data = resp.json()
                return self._build_usage_from_oauth(account.id, usage_data)

        except httpx.HTTPError as e:
            logger.exception("Failed to fetch Claude usage for %s", account.label)
            return UsageData(
                account_id=account.id,
                provider=Provider.CLAUDE,
                error=f"Connection error: {type(e).__name__}",
            )

    async def _refresh_and_retry(self, account: Account, secret: str) -> str | None:
        """Try to refresh the OAuth token using our stored refresh_token.

        Returns the new access token on success, None on failure.
        """
        from ai_usage.adapters.claude.auth import ClaudeAuth

        auth = ClaudeAuth()
        try:
            new_cred = await auth.refresh_credential(account)
            if new_cred:
                # Re-read the updated secret
                new_secret = get_secret(new_cred.keyring_key)
                if new_secret:
                    return self._extract_access_token(new_secret)
        except Exception as e:
            logger.debug("Independent token refresh failed: %s", e)
        return None

    def _parse_api_error(self, resp: httpx.Response) -> str:
        """Parse an API error response into a human-friendly message."""
        try:
            data = resp.json()
            error = data.get("error", {})
            if isinstance(error, dict):
                error_type = error.get("type", "")
                error_msg = error.get("message", "")
                if error_type == "rate_limit_error":
                    return "Rate limited — try again in a few minutes"
                if error_type == "authentication_error":
                    return "Authentication failed — run 'ai-usage accounts login <id> --browser'"
                if error_msg:
                    # Truncate long messages
                    if len(error_msg) > 80:
                        return f"{error_msg[:77]}..."
                    return error_msg
            elif isinstance(error, str):
                if error == "invalid_grant":
                    desc = data.get("error_description", "")
                    return f"Token revoked — run 'ai-usage accounts login <id> --browser'"
                return error
            return f"API error ({resp.status_code})"
        except Exception:
            return f"API error ({resp.status_code})"

    def _extract_access_token(self, secret: str) -> str | None:
        """Extract access token from stored secret (JSON or plain)."""
        try:
            data = json.loads(secret)
            return data.get("access_token")
        except (json.JSONDecodeError, AttributeError):
            return secret if secret.startswith("sk-ant-") else None

    async def _fetch_usage_cookie(self, account: Account, headers: dict) -> UsageData:
        """Fetch usage via claude.ai with session cookies (legacy)."""
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                # Get organizations
                orgs = await self._fetch_organizations(client, headers)
                if not orgs:
                    return UsageData(
                        account_id=account.id,
                        provider=Provider.CLAUDE,
                        error="No organizations found (Cloudflare may be blocking)",
                    )

                org_id = orgs[0]["uuid"]
                usage_data = await self._fetch_org_usage(client, headers, org_id)
                overage_data = await self._fetch_overage(client, headers, org_id)

                return self._build_usage_data(
                    account_id=account.id,
                    account_info={},
                    usage_data=usage_data,
                    overage_data=overage_data,
                    org_data=orgs[0],
                )
        except AuthenticationError:
            raise
        except httpx.HTTPError as e:
            return UsageData(
                account_id=account.id,
                provider=Provider.CLAUDE,
                error=f"HTTP error: {e}",
            )

    def _build_headers(self, auth_method: AuthMethod, secret: str) -> dict[str, str]:
        """Build HTTP headers based on auth method."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ai-usage/0.1.0",
        }

        if auth_method == AuthMethod.OAUTH_TOKEN:
            # Extract access token from stored JSON
            try:
                data = json.loads(secret)
                token = data.get("access_token", secret)
            except (json.JSONDecodeError, AttributeError):
                token = secret
            headers["Authorization"] = f"Bearer {token}"
            headers["anthropic-beta"] = "oauth-2025-04-20"
        elif auth_method == AuthMethod.SESSION_COOKIE:
            headers["Cookie"] = f"sessionKey={secret}"
        else:
            # Fallback: try as bearer token
            headers["Authorization"] = f"Bearer {secret}"

        return headers

    async def _fetch_account(self, client: httpx.AsyncClient, headers: dict) -> dict:
        """Fetch account info (email, plan)."""
        try:
            resp = await client.get(f"{CLAUDE_API_BASE}/api/account", headers=headers)
            if resp.status_code == 401 or resp.status_code == 403:
                raise AuthenticationError(
                    provider="claude", message="Authentication expired or invalid"
                )
            resp.raise_for_status()
            data = resp.json()
            return {
                "email": data.get("email_address", data.get("email")),
                "plan_name": self._extract_plan_name(data),
                "raw": data,
            }
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise AuthenticationError(
                    provider="claude", message="Authentication expired or invalid"
                )
            logger.warning("Failed to fetch account info: %s", e)
            return {}

    async def _fetch_organizations(self, client: httpx.AsyncClient, headers: dict) -> list[dict]:
        """Fetch organization list."""
        try:
            resp = await client.get(f"{CLAUDE_API_BASE}/api/organizations", headers=headers)
            if resp.status_code in (401, 403):
                raise AuthenticationError(
                    provider="claude", message="Authentication expired or invalid"
                )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise AuthenticationError(
                    provider="claude", message="Authentication expired or invalid"
                )
            logger.warning("Failed to fetch organizations: %s", e)
            return []

    async def _fetch_org_usage(self, client: httpx.AsyncClient, headers: dict, org_id: str) -> dict:
        """Fetch usage data for an organization (cookie auth only)."""
        try:
            resp = await client.get(
                f"{CLAUDE_API_BASE}/api/organizations/{org_id}/usage",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("Failed to fetch usage: %s", e)
            return {}

    async def _fetch_overage(self, client: httpx.AsyncClient, headers: dict, org_id: str) -> dict:
        """Fetch overage/extra usage spend limit."""
        try:
            resp = await client.get(
                f"{CLAUDE_API_BASE}/api/organizations/{org_id}/overage_spend_limit",
                headers=headers,
            )
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            pass
        return {}

    def _extract_plan_name(self, account_data: dict) -> str | None:
        """Extract plan name from account data."""
        # Claude returns plan info in various formats
        memberships = account_data.get("memberships", [])
        if memberships:
            org = memberships[0].get("organization", {})
            billing = org.get("billing", {})
            plan = billing.get("plan_display_name") or billing.get("plan")
            if plan:
                return plan

        # Direct plan field
        return (
            account_data.get("plan", {}).get("name")
            if isinstance(account_data.get("plan"), dict)
            else account_data.get("plan")
        )

    def _build_usage_from_oauth(self, account_id: str, data: dict) -> UsageData:
        """Build UsageData from the /api/oauth/usage response.

        Response format:
        {
          "five_hour": {"utilization": 1.0, "resets_at": "2026-..."},
          "seven_day": {"utilization": 50.0, "resets_at": "2026-..."},
          "seven_day_opus": {"utilization": ..., "resets_at": ...} | null,
          "seven_day_sonnet": {...} | null,
          "seven_day_cowork": {...} | null,
          "seven_day_oauth_apps": {...} | null,
          "extra_usage": {
            "is_enabled": true,
            "monthly_limit": 5000,
            "used_credits": 432.0,
            "utilization": 8.64
          } | null,
          "iguana_necktie": {...} | null
        }
        """
        quotas: list[Quota] = []
        model_breakdown: list[ModelBreakdown] = []
        raw_data = {"oauth_usage": data}

        # Quota name mapping for human-friendly display
        quota_names = {
            "five_hour": "5-Hour",
            "seven_day": "Weekly",
            "seven_day_opus": "Weekly (Opus)",
            "seven_day_sonnet": "Weekly (Sonnet)",
            "seven_day_cowork": "Weekly (Cowork)",
            "seven_day_oauth_apps": "Weekly (OAuth Apps)",
            "iguana_necktie": "Special",
        }

        for key, friendly_name in quota_names.items():
            value = data.get(key)
            if not value or not isinstance(value, dict):
                continue

            utilization = value.get("utilization")
            if utilization is None:
                continue

            quotas.append(
                Quota(
                    name=friendly_name,
                    used=float(utilization),
                    limit=100.0,
                    unit="percent",
                    reset_at=self._parse_reset_time(value.get("resets_at")),
                )
            )

            # Track per-model quotas as breakdown
            if key.startswith("seven_day_") and key != "seven_day_oauth_apps":
                model_name = key.replace("seven_day_", "").replace("_", " ").title()
                model_breakdown.append(
                    ModelBreakdown(model_name=model_name, usage_percent=float(utilization))
                )

        # Extra usage (overage/credits)
        extra = data.get("extra_usage")
        if extra and isinstance(extra, dict) and extra.get("is_enabled"):
            used = extra.get("used_credits", 0)
            limit = extra.get("monthly_limit", 0)
            quotas.append(
                Quota(
                    name="Extra Credits",
                    used=float(used),
                    limit=float(limit) if limit else None,
                    unit="credits",
                )
            )

        # Extract plan from stored credential data
        plan_name = None
        # Try to read subscription_type from our stored token data
        try:
            from ai_usage.adapters.storage.file import get_secret

            secret = get_secret(f"claude-oauth-{account_id}")
            if secret:
                token_data = json.loads(secret)
                sub_type = token_data.get("subscription_type")
                if sub_type:
                    plan_name = f"Claude {sub_type.title()}"
        except Exception:
            pass

        return UsageData(
            account_id=account_id,
            provider=Provider.CLAUDE,
            plan_name=plan_name,
            quotas=quotas,
            model_breakdown=model_breakdown,
            raw_data=raw_data,
        )

    def _build_usage_data(
        self,
        account_id: str,
        account_info: dict,
        usage_data: dict,
        overage_data: dict,
        org_data: dict,
    ) -> UsageData:
        """Build structured UsageData from raw API responses."""
        quotas: list[Quota] = []
        model_breakdown: list[ModelBreakdown] = []

        # Parse usage data — Claude returns various formats depending on the plan
        # Common fields: session usage, weekly usage, model-specific usage
        if usage_data:
            # Session quota
            session_usage = usage_data.get("session_usage")
            if session_usage is not None:
                quotas.append(
                    Quota(
                        name="session",
                        used=float(session_usage),
                        limit=100.0,  # Percentage-based
                        unit="percent",
                        reset_at=self._parse_reset_time(usage_data.get("session_reset_at")),
                    )
                )

            # Weekly quota
            weekly_usage = usage_data.get("weekly_usage")
            if weekly_usage is not None:
                quotas.append(
                    Quota(
                        name="weekly",
                        used=float(weekly_usage),
                        limit=100.0,
                        unit="percent",
                        reset_at=self._parse_reset_time(usage_data.get("weekly_reset_at")),
                    )
                )

            # Daily quota (some plans)
            daily_usage = usage_data.get("daily_usage")
            if daily_usage is not None:
                quotas.append(
                    Quota(
                        name="daily",
                        used=float(daily_usage),
                        limit=100.0,
                        unit="percent",
                        reset_at=self._parse_reset_time(usage_data.get("daily_reset_at")),
                    )
                )

            # Model-specific breakdowns
            for key, value in usage_data.items():
                if key.endswith("_usage") and key not in (
                    "session_usage",
                    "weekly_usage",
                    "daily_usage",
                ):
                    model_name = key.replace("_usage", "").replace("_", " ").title()
                    if isinstance(value, (int, float)):
                        model_breakdown.append(
                            ModelBreakdown(
                                model_name=model_name,
                                usage_percent=float(value),
                            )
                        )

        # Extract plan name from org data if not in account info
        plan_name = account_info.get("plan_name")
        if not plan_name and org_data:
            billing = org_data.get("billing", {})
            plan_name = billing.get("plan_display_name") or billing.get("plan")

        return UsageData(
            account_id=account_id,
            provider=Provider.CLAUDE,
            plan_name=plan_name,
            quotas=quotas,
            model_breakdown=model_breakdown,
            raw_data={
                "account": account_info,
                "usage": usage_data,
                "overage": overage_data,
                "org": org_data,
            },
        )

    def _parse_reset_time(self, value: str | None) -> datetime | None:
        """Parse ISO format reset time."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
