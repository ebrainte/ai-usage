"""Usage service — orchestrates fetching usage data across all accounts.

This is the main application service. The UI layer talks to this.
It handles parallel fetching, caching, and scheduling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ai_usage.adapters.claude.usage import ClaudeUsage
from ai_usage.adapters.copilot.usage import CopilotUsage
from ai_usage.adapters.chatgpt.usage import ChatGPTUsage
from ai_usage.app.account_manager import AccountManager
from ai_usage.domain.models import AccountStatus, Provider, UsageData

logger = logging.getLogger(__name__)


class UsageService:
    """Fetches and caches usage data for all configured accounts."""

    def __init__(self, account_manager: AccountManager | None = None):
        self.account_manager = account_manager or AccountManager()
        self._usage_adapters = {
            Provider.CLAUDE: ClaudeUsage(),
            Provider.COPILOT: CopilotUsage(),
            Provider.CHATGPT: ChatGPTUsage(),
        }
        # Cache of last fetched data per account
        self._cache: dict[str, UsageData] = {}

    async def fetch_all(self) -> list[UsageData]:
        """Fetch usage data for ALL configured accounts in parallel."""
        accounts = self.account_manager.list_accounts()
        active_accounts = [
            a for a in accounts if a.status in (AccountStatus.ACTIVE, AccountStatus.AUTH_EXPIRED)
        ]

        if not active_accounts:
            return []

        tasks = [self.fetch_one(a.id) for a in active_accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        usage_list: list[UsageData] = []
        for account, result in zip(active_accounts, results):
            if isinstance(result, Exception):
                logger.error("Failed to fetch usage for %s: %s", account.id, result)
                usage_data = UsageData(
                    account_id=account.id,
                    provider=account.provider,
                    error=str(result),
                )
            else:
                usage_data = result

            self._cache[account.id] = usage_data
            usage_list.append(usage_data)

        return usage_list

    async def fetch_one(self, account_id: str) -> UsageData:
        """Fetch usage data for a single account."""
        account = self.account_manager.get_account(account_id)
        if not account:
            return UsageData(
                account_id=account_id,
                provider=Provider.CLAUDE,  # placeholder
                error=f"Account '{account_id}' not found",
            )

        adapter = self._usage_adapters.get(account.provider)
        if not adapter:
            return UsageData(
                account_id=account_id,
                provider=account.provider,
                error=f"No usage adapter for provider: {account.provider}",
            )

        try:
            usage_data = await adapter.fetch_usage(account)

            # Update account email from fetched data if available
            if usage_data.raw_data:
                email = usage_data.raw_data.get("account", {}).get(
                    "email"
                ) or usage_data.raw_data.get("user", {}).get("email")
                if email and email != account.email:
                    account.email = email
                    account.last_refreshed = datetime.now().astimezone()
                    self.account_manager.update_account(account)

            # Update status based on result
            if usage_data.is_error and "auth" in (usage_data.error or "").lower():
                account.status = AccountStatus.AUTH_EXPIRED
                self.account_manager.update_account(account)

            self._cache[account_id] = usage_data
            return usage_data

        except Exception as e:
            logger.exception("Error fetching usage for %s", account_id)
            error_data = UsageData(
                account_id=account_id,
                provider=account.provider,
                error=str(e),
            )
            self._cache[account_id] = error_data
            return error_data

    def get_cached(self, account_id: str) -> UsageData | None:
        """Get last fetched usage data from cache."""
        return self._cache.get(account_id)

    def get_all_cached(self) -> list[UsageData]:
        """Get all cached usage data."""
        return list(self._cache.values())

    def clear_cache(self) -> None:
        """Clear the usage cache."""
        self._cache.clear()
