"""GitHub Copilot usage data fetcher.

Uses the internal GitHub Copilot API:
- GET /copilot_internal/user — usage data (spoofs VS Code headers)
- GET /user — account info

This is an undocumented API used by VS Code and other Copilot clients.
"""

from __future__ import annotations

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

GITHUB_API_BASE = "https://api.github.com"

# Headers that mimic VS Code Copilot client
COPILOT_HEADERS = {
    "Editor-Version": "vscode/1.96.2",
    "Editor-Plugin-Version": "copilot/1.200.0",
    "User-Agent": "GithubCopilot/1.200.0",
    "Accept": "application/json",
}


class CopilotUsage:
    """Fetches GitHub Copilot usage data."""

    def supports_provider(self) -> str:
        return Provider.COPILOT

    async def fetch_usage(self, account: Account) -> UsageData:
        """Fetch usage data for a GitHub Copilot account."""
        if not account.credential:
            return UsageData(
                account_id=account.id,
                provider=Provider.COPILOT,
                error="No credentials configured",
            )

        token = get_secret(account.credential.keyring_key)
        if not token:
            return UsageData(
                account_id=account.id,
                provider=Provider.COPILOT,
                error="Credentials not found in keyring",
            )

        headers = {
            **COPILOT_HEADERS,
            "Authorization": f"token {token}",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Step 1: Get user info
                user_info = await self._fetch_user(client, token)

                # Step 2: Get Copilot-specific usage
                copilot_data = await self._fetch_copilot_user(client, headers)

                # Step 3: Try to get the Copilot token for more detailed info
                copilot_token_data = await self._fetch_copilot_token(client, headers)

                return self._build_usage_data(
                    account_id=account.id,
                    user_info=user_info,
                    copilot_data=copilot_data,
                    token_data=copilot_token_data,
                )

        except AuthenticationError:
            raise
        except httpx.HTTPError as e:
            logger.exception("Failed to fetch Copilot usage for %s", account.label)
            return UsageData(
                account_id=account.id,
                provider=Provider.COPILOT,
                error=f"HTTP error: {e}",
            )

    async def _fetch_user(self, client: httpx.AsyncClient, token: str) -> dict:
        """Fetch GitHub user info."""
        try:
            resp = await client.get(
                f"{GITHUB_API_BASE}/user",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/json",
                },
            )
            if resp.status_code in (401, 403):
                raise AuthenticationError(
                    provider="copilot", message="GitHub token expired or invalid"
                )
            resp.raise_for_status()
            data = resp.json()
            return {
                "login": data.get("login"),
                "email": data.get("email"),
                "name": data.get("name"),
            }
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise AuthenticationError(
                    provider="copilot", message="GitHub token expired or invalid"
                )
            logger.warning("Failed to fetch user info: %s", e)
            return {}

    async def _fetch_copilot_user(self, client: httpx.AsyncClient, headers: dict) -> dict:
        """Fetch Copilot-specific user data (internal API)."""
        try:
            resp = await client.get(
                f"{GITHUB_API_BASE}/copilot_internal/user",
                headers=headers,
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                # Copilot not enabled for this user
                return {"error": "Copilot not enabled"}
            elif resp.status_code in (401, 403):
                raise AuthenticationError(provider="copilot", message="Not authorized for Copilot")
            else:
                logger.warning(
                    "Copilot internal API returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return {}
        except httpx.HTTPStatusError as e:
            logger.warning("Failed to fetch Copilot user data: %s", e)
            return {}

    async def _fetch_copilot_token(self, client: httpx.AsyncClient, headers: dict) -> dict:
        """Fetch a Copilot session token (contains quota info)."""
        try:
            resp = await client.get(
                f"{GITHUB_API_BASE}/copilot_internal/v2/token",
                headers=headers,
            )
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            pass
        return {}

    def _build_usage_data(
        self,
        account_id: str,
        user_info: dict,
        copilot_data: dict,
        token_data: dict,
    ) -> UsageData:
        """Build structured UsageData from raw API responses."""
        quotas: list[Quota] = []
        model_breakdown: list[ModelBreakdown] = []

        if copilot_data.get("error"):
            return UsageData(
                account_id=account_id,
                provider=Provider.COPILOT,
                error=copilot_data["error"],
                raw_data={"user": user_info, "copilot": copilot_data},
            )

        plan_name = None

        if copilot_data:
            # Plan name
            copilot_plan = copilot_data.get("copilot_plan")
            if copilot_plan:
                plan_name = f"Copilot {copilot_plan.title()}"

            # Parse quota_reset_date_utc
            reset_at = None
            reset_str = copilot_data.get("quota_reset_date_utc")
            if reset_str:
                try:
                    reset_at = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Parse quota_snapshots (the real data format from the API)
            snapshots = copilot_data.get("quota_snapshots", {})
            for quota_id, snapshot in snapshots.items():
                entitlement = snapshot.get("entitlement", 0)
                remaining = snapshot.get("remaining", 0)
                unlimited = snapshot.get("unlimited", False)
                pct_remaining = snapshot.get("percent_remaining", 100.0)

                if unlimited:
                    # Unlimited quotas — just note them
                    quotas.append(
                        Quota(
                            name=quota_id,
                            limit=None,  # unlimited
                            used=0,
                            unit=quota_id,
                            reset_at=reset_at,
                        )
                    )
                elif entitlement > 0:
                    used = entitlement - remaining
                    quotas.append(
                        Quota(
                            name=quota_id,
                            limit=float(entitlement),
                            used=float(used),
                            remaining=float(remaining),
                            unit=quota_id.replace("_", " "),
                            reset_at=reset_at,
                        )
                    )

            # Fallback: Extract premium_requests directly if no snapshots
            if not snapshots:
                premium_requests = copilot_data.get("premium_requests")
                if premium_requests:
                    limit = premium_requests.get("limit")
                    used = premium_requests.get("used", 0)
                    remaining_val = premium_requests.get("remaining")

                    quotas.append(
                        Quota(
                            name="premium_requests",
                            limit=float(limit) if limit else None,
                            used=float(used),
                            remaining=float(remaining_val) if remaining_val is not None else None,
                            unit="requests",
                            reset_at=reset_at,
                        )
                    )

        if not plan_name:
            plan_name = "Copilot"

        # Filter out unlimited quotas from display (keep only those with actual limits)
        display_quotas = [q for q in quotas if q.limit is not None]
        # If all are unlimited, show them anyway
        if not display_quotas:
            display_quotas = quotas

        return UsageData(
            account_id=account_id,
            provider=Provider.COPILOT,
            plan_name=plan_name,
            quotas=display_quotas,
            model_breakdown=model_breakdown,
            raw_data={
                "user": user_info,
                "copilot": copilot_data,
                "token": token_data,
            },
        )
