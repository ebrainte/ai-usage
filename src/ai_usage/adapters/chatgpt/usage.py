"""ChatGPT usage data fetcher.

Uses ChatGPT internal APIs (same as web dashboard):
- /backend-api/me — account info
- /backend-api/accounts/check/v4-2023-04-27 — plan + usage info

Note: These are undocumented internal APIs. They may change without notice.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ai_usage.adapters.storage.file import get_secret
from ai_usage.domain.exceptions import AuthenticationError
from ai_usage.domain.models import (
    Account,
    AuthMethod,
    Provider,
    Quota,
    UsageData,
)

logger = logging.getLogger(__name__)

CHATGPT_BASE = "https://chatgpt.com"


class ChatGPTUsage:
    """Fetches ChatGPT usage data."""

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

        token = get_secret(account.credential.keyring_key)
        if not token:
            return UsageData(
                account_id=account.id,
                provider=Provider.CHATGPT,
                error="Credentials not found in keyring",
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ai-usage/0.1.0",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Step 1: Get user info
                user_info = await self._fetch_me(client, headers)

                # Step 2: Get account/plan details
                account_info = await self._fetch_account_check(client, headers)

                return self._build_usage_data(
                    account_id=account.id,
                    user_info=user_info,
                    account_info=account_info,
                )

        except AuthenticationError:
            raise
        except httpx.HTTPError as e:
            logger.exception("Failed to fetch ChatGPT usage for %s", account.label)
            return UsageData(
                account_id=account.id,
                provider=Provider.CHATGPT,
                error=f"HTTP error: {e}",
            )

    async def _fetch_me(self, client: httpx.AsyncClient, headers: dict) -> dict:
        """Fetch user profile."""
        try:
            resp = await client.get(
                f"{CHATGPT_BASE}/backend-api/me",
                headers=headers,
            )
            if resp.status_code in (401, 403):
                raise AuthenticationError(provider="chatgpt", message="Session expired or invalid")
            resp.raise_for_status()
            data = resp.json()
            return {
                "email": data.get("email"),
                "name": data.get("name"),
                "raw": data,
            }
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise AuthenticationError(provider="chatgpt", message="Session expired or invalid")
            return {}

    async def _fetch_account_check(self, client: httpx.AsyncClient, headers: dict) -> dict:
        """Fetch account/plan/usage info."""
        try:
            resp = await client.get(
                f"{CHATGPT_BASE}/backend-api/accounts/check/v4-2023-04-27",
                headers=headers,
            )
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch account check: %s", e)
        return {}

    def _build_usage_data(
        self,
        account_id: str,
        user_info: dict,
        account_info: dict,
    ) -> UsageData:
        """Build structured UsageData from raw API responses."""
        quotas: list[Quota] = []
        plan_name = None

        # Parse account info
        if account_info:
            accounts = account_info.get("accounts", {})
            if accounts:
                # Usually one account entry
                for acct_id, acct_data in accounts.items():
                    entitlements = acct_data.get("entitlement", {})
                    plan = entitlements.get("subscription_plan")
                    if plan:
                        plan_name = plan

                    # Rate limits from entitlements
                    rate_limits = acct_data.get("rate_limits", [])
                    for rl in rate_limits:
                        limit = rl.get("limit")
                        remaining = rl.get("remaining")
                        window = rl.get("window")

                        quotas.append(
                            Quota(
                                name=f"rate_{window}" if window else "rate_limit",
                                limit=float(limit) if limit else None,
                                remaining=float(remaining) if remaining is not None else None,
                                unit="messages",
                            )
                        )

        if not plan_name:
            plan_name = "ChatGPT"

        return UsageData(
            account_id=account_id,
            provider=Provider.CHATGPT,
            plan_name=plan_name,
            quotas=quotas,
            raw_data={"user": user_info, "account": account_info},
        )
