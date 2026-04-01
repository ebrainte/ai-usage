"""Dashboard screen — main view showing all account usage cards."""

from __future__ import annotations

import logging

from textual import work
from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, Static

from ai_usage.app.usage_service import UsageService
from ai_usage.app.account_manager import AccountManager
from ai_usage.domain.models import AccountStatus, UsageData
from ai_usage.ui.tui.widgets.usage_card import UsageCard

logger = logging.getLogger(__name__)


class StatusBar(Static):
    """Bottom status bar showing last refresh time and auto-refresh state."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._status_text = "Press [bold]r[/bold] to refresh"

    def compose(self) -> ComposeResult:
        yield Label(self._status_text, id="status-text")

    def set_status(self, text: str) -> None:
        self._status_text = text
        try:
            label = self.query_one("#status-text", Label)
            label.update(text)
        except Exception:
            pass


class DashboardScreen(Screen):
    """Main dashboard screen."""

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("a", "accounts", "Accounts"),
        ("t", "cycle_refresh", "Refresh Timer"),
        ("q", "quit", "Quit"),
    ]

    REFRESH_OPTIONS = [
        (60, "1m"),
        (300, "5m"),
        (600, "10m"),
        (3600, "1h"),
    ]

    CSS = """
    DashboardScreen {
        background: $surface;
    }

    #card-container {
        padding: 0 1;
    }

    .usage-card {
        border: solid $primary;
        padding: 0 1;
        margin-bottom: 0;
        background: $panel;
        height: auto;
    }

    .card-header {
        height: 1;
    }

    .card-title {
        width: 1fr;
    }

    .card-plan {
        color: $text-muted;
        text-align: right;
        width: auto;
    }

    .card-error {
        margin: 0 1;
    }

    .card-empty {
        color: $text-muted;
    }

    .card-models {
        color: $text-muted;
    }

    .card-loading {
        color: $text-muted;
    }

    .quota-row {
        height: auto;
        margin: 0;
        padding: 0;
    }

    .quota-info {
        height: 1;
    }

    .quota-label {
        width: 1fr;
    }

    .quota-pct {
        width: 6;
        text-align: right;
    }

    .quota-bar {
        margin: 0 2 0 2;
        height: 1;
    }

    .quota-bar Bar {
        width: 1fr;
    }

    .quota-bar .bar--bar {
        color: $accent;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        padding: 0 2;
        background: $primary-background;
        color: $text;
    }

    #no-accounts {
        text-align: center;
        margin: 4;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        usage_service: UsageService,
        account_manager: AccountManager,
        auto_refresh_seconds: int = 300,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.usage_service = usage_service
        self.account_manager = account_manager
        self.auto_refresh_seconds = auto_refresh_seconds
        self._cards: dict[str, UsageCard] = {}
        self._refresh_timer = None
        # Find the initial refresh option index
        self._refresh_idx = 1  # default 5m
        for i, (secs, _) in enumerate(self.REFRESH_OPTIONS):
            if secs == auto_refresh_seconds:
                self._refresh_idx = i
                break

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ScrollableContainer(id="card-container")
        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.load_data()
        self._start_refresh_timer()

    @work(exclusive=True, thread=False)
    async def load_data(self) -> None:
        """Load usage data for all accounts."""
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.set_status("Refreshing...")

        container = self.query_one("#card-container", ScrollableContainer)

        accounts = self.account_manager.list_accounts()
        if not accounts:
            # Clear existing cards and show message
            await container.remove_children()
            await container.mount(
                Label(
                    "[bold]No accounts configured[/bold]\n\n"
                    "Press [bold]a[/bold] to add accounts, or use the CLI:\n"
                    "  ai-usage accounts add --provider claude --label 'Personal'\n"
                    "  ai-usage accounts add --provider copilot --label 'Work'\n"
                    "  ai-usage accounts login <account-id>",
                    id="no-accounts",
                )
            )
            status_bar.set_status("No accounts configured. Press [bold]a[/bold] to add.")
            return

        # Fetch all usage data (only for active/auth_expired accounts)
        results = await self.usage_service.fetch_all()

        # Build a map for quick lookup
        results_map: dict[str, UsageData] = {r.account_id: r for r in results}

        # Update or create cards
        await container.remove_children()
        self._cards.clear()

        for account in accounts:
            data = results_map.get(account.id)

            # For unconfigured accounts, show a helpful message
            if data is None and account.status == AccountStatus.UNCONFIGURED:
                data = UsageData(
                    account_id=account.id,
                    provider=account.provider,
                    error="Not logged in — press [bold]a[/bold] to manage accounts",
                )

            card = UsageCard(
                usage_data=data,
                account_label=account.display_name,
                classes="usage-card",
                id=f"card-{account.id}",
            )
            self._cards[account.id] = card
            await container.mount(card)

        self._update_status_bar()

    def _start_refresh_timer(self) -> None:
        """Start or restart the auto-refresh timer."""
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        self._refresh_timer = self.set_interval(self.auto_refresh_seconds, self._auto_refresh)

    def _auto_refresh(self) -> None:
        """Auto-refresh callback."""
        self.load_data()

    def _update_status_bar(self) -> None:
        """Update the status bar with current time and refresh interval."""
        from datetime import datetime

        now = datetime.now().strftime("%H:%M:%S")
        _, label = self.REFRESH_OPTIONS[self._refresh_idx]
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.set_status(
            f"Last: {now}  |  Auto: {label}  |  "
            f"[bold]r[/bold] Refresh  [bold]t[/bold] Timer  "
            f"[bold]a[/bold] Accounts  [bold]q[/bold] Quit"
        )

    def action_refresh(self) -> None:
        """Manual refresh."""
        self.load_data()

    def action_cycle_refresh(self) -> None:
        """Cycle through refresh interval options."""
        self._refresh_idx = (self._refresh_idx + 1) % len(self.REFRESH_OPTIONS)
        self.auto_refresh_seconds, label = self.REFRESH_OPTIONS[self._refresh_idx]
        self._start_refresh_timer()
        self._update_status_bar()
        self.notify(f"Auto-refresh set to {label}", timeout=3)

    def on_screen_resume(self) -> None:
        """Reload data when returning from another screen (e.g., Accounts)."""
        logger.info("Dashboard resumed — reloading accounts + usage")
        self.load_data()

    def action_accounts(self) -> None:
        """Show account management screen."""
        self.app.push_screen("accounts")

    def action_quit(self) -> None:
        """Quit the app."""
        self.app.exit()
