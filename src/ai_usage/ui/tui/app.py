"""Main Textual TUI application."""

from __future__ import annotations

from textual.app import App

from ai_usage.app.account_manager import AccountManager
from ai_usage.app.usage_service import UsageService
from ai_usage.ui.tui.screens.dashboard import DashboardScreen
from ai_usage.ui.tui.screens.accounts import AccountsScreen


class AiUsageApp(App):
    """AI Usage Tracker — Terminal UI."""

    TITLE = "AI Usage Tracker"
    SUB_TITLE = "Claude | ChatGPT | Copilot"

    CSS = """
    Screen {
        background: $surface;
    }
    """

    def __init__(self, auto_refresh_seconds: int = 300, **kwargs):
        super().__init__(**kwargs)
        self.account_manager = AccountManager()
        self.usage_service = UsageService(account_manager=self.account_manager)
        self.auto_refresh_seconds = auto_refresh_seconds

    def on_mount(self) -> None:
        # Install the accounts screen (not using SCREENS to inject dependencies)
        self.install_screen(
            AccountsScreen(account_manager=self.account_manager),
            name="accounts",
        )

        # Push the dashboard as the main screen
        dashboard = DashboardScreen(
            usage_service=self.usage_service,
            account_manager=self.account_manager,
            auto_refresh_seconds=self.auto_refresh_seconds,
        )
        self.push_screen(dashboard)
