"""ChatGPT usage data fetcher.

Uses ChatGPT's internal wham/usage API (same endpoint as CodexBar):
- GET /backend-api/wham/usage — rate limits, plan type, credits

Supports both OAuth tokens (from Codex import) and session tokens.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from ai_usage.adapters.storage.file import get_secret
from ai_usage.domain.exceptions import AuthenticationError
from ai_usage.domain.models import (
    Account,
    Provider,
    Quota,
    UsageData,
)

logger = logging.getLogger(__name__)

CHATGPT_BASE = "https://chatgpt.com"

# Known plan types from OpenAI
PLAN_DISPLAY_NAMES = {
    "free": "ChatGPT Free",
    "go": "ChatGPT Go",
    "plus": "ChatGPT Plus",
    "pro": "ChatGPT Pro",
    "team": "ChatGPT Team",
    "business": "ChatGPT Business",
    "enterprise": "ChatGPT Enterprise",
    "education": "ChatGPT Education",
}


class ChatGPTUsage:
    """Fetches ChatGPT usage data via wham/usage API."""

    def supports_provider(self) -> str:
        return Provider.CHATGPT

    async def fetch_usage(self, account: Account) -> UsageData:
        """Fetch usage data for a ChatGPT account."""
        if not account.credential:
            return UsageData(
                account_id=account.id,
                provider=Provider.CHATGPT,
                error="No credentials configured",
            )

        secret = get_secret(account.credential.keyring_key)
        if not secret:
            return UsageData(
                account_id=account.id,
                provider=Provider.CHATGPT,
                error="Credentials not found in keyring",
            )

        # Parse token — could be JSON (OAuth) or plain string (session token)
        access_token, openai_account_id = self._extract_token(secret)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "ai-usage",
            "Accept": "application/json",
        }
        if openai_account_id:
            headers["ChatGPT-Account-Id"] = openai_account_id

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CHATGPT_BASE}/backend-api/wham/usage",
                    headers=headers,
                )

                if resp.status_code in (401, 403):
                    return UsageData(
                        account_id=account.id,
                        provider=Provider.CHATGPT,
                        error="Session expired — re-import from Codex or provide new token",
                    )

                resp.raise_for_status()
                data = resp.json()
                logger.debug(
                    "wham/usage response for %s: %s", account.label, json.dumps(data, indent=2)
                )

                return self._parse_wham_usage(account.id, data)

        except httpx.HTTPError as e:
            logger.exception("Failed to fetch ChatGPT usage for %s", account.label)
            return UsageData(
                account_id=account.id,
                provider=Provider.CHATGPT,
                error=f"HTTP error: {e}",
            )

    def _extract_token(self, secret: str) -> tuple[str, str | None]:
        """Extract access token and optional account ID from stored secret."""
        try:
            data = json.loads(secret)
            return data.get("access_token", secret), data.get("account_id")
        except (json.JSONDecodeError, TypeError):
            return secret, None

    def _parse_wham_usage(self, account_id: str, data: dict) -> UsageData:
        """Parse the wham/usage response into UsageData.

        Response format (from CodexBar analysis):
        {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 42,
                    "reset_at": 1743523200,
                    "limit_window_seconds": 18000
                },
                "secondary_window": {
                    "used_percent": 10,
                    "reset_at": 1743782400,
                    "limit_window_seconds": 604800
                }
            },
            "credits": {
                "has_credits": true,
                "unlimited": false,
                "balance": 123.45
            }
        }
        """
        quotas: list[Quota] = []

        # Plan type
        plan_type = data.get("plan_type", "unknown")
        plan_name = PLAN_DISPLAY_NAMES.get(plan_type, f"ChatGPT {plan_type.title()}")

        # Rate limits
        rate_limit = data.get("rate_limit", {})

        primary = rate_limit.get("primary_window")
        if primary:
            used_pct = primary.get("used_percent", 0)
            reset_at = primary.get("reset_at")
            window_secs = primary.get("limit_window_seconds", 18000)

            reset_dt = None
            if reset_at:
                try:
                    reset_dt = datetime.fromtimestamp(reset_at, tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            # Window label: 18000s = 5h, etc.
            window_hours = window_secs // 3600
            window_label = f"{window_hours}-Hour" if window_hours else f"{window_secs}s"

            quotas.append(
                Quota(
                    name=window_label,
                    limit=100.0,
                    used=float(used_pct),
                    unit="percent",
                    reset_at=reset_dt,
                )
            )

        secondary = rate_limit.get("secondary_window")
        if secondary:
            used_pct = secondary.get("used_percent", 0)
            reset_at = secondary.get("reset_at")
            window_secs = secondary.get("limit_window_seconds", 604800)

            reset_dt = None
            if reset_at:
                try:
                    reset_dt = datetime.fromtimestamp(reset_at, tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            window_days = window_secs // 86400
            window_label = f"{window_days}-Day" if window_days else "Weekly"

            quotas.append(
                Quota(
                    name=window_label,
                    limit=100.0,
                    used=float(used_pct),
                    unit="percent",
                    reset_at=reset_dt,
                )
            )

        # Credits
        credits = data.get("credits", {})
        if credits.get("has_credits") and not credits.get("unlimited"):
            balance = credits.get("balance", 0)
            # Balance can be a string or number (CodexBar handles both)
            if isinstance(balance, str):
                try:
                    balance = float(balance)
                except ValueError:
                    balance = 0.0
            quotas.append(
                Quota(
                    name="Credits",
                    used=0.0,
                    remaining=float(balance),
                    unit="credits",
                )
            )

        return UsageData(
            account_id=account_id,
            provider=Provider.CHATGPT,
            plan_name=plan_name,
            quotas=quotas,
            raw_data=data,
        )
