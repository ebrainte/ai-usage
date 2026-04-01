"""Account management screen — add, remove, and login to accounts."""

from __future__ import annotations

import logging

from textual import work
from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from ai_usage.app.account_manager import AccountManager
from ai_usage.domain.models import Account, Provider

logger = logging.getLogger(__name__)


class AccountRow(Static):
    """A row showing one account with actions."""

    def __init__(self, account: Account, **kwargs):
        super().__init__(**kwargs)
        self.account = account

    def compose(self) -> ComposeResult:
        a = self.account
        status_color = {
            "active": "green",
            "auth_expired": "yellow",
            "error": "red",
            "unconfigured": "dim",
        }.get(a.status, "dim")

        yield Horizontal(
            Label(f"[bold]{a.provider.value.upper():8s}[/bold]", classes="acct-provider"),
            Label(f"{a.label:20s}", classes="acct-label"),
            Label(
                f"{a.credential.auth_method.value if a.credential else 'not logged in':30s}",
                classes="acct-email",
            ),
            Label(f"[{status_color}]{a.status.value}[/{status_color}]", classes="acct-status"),
            Button("Login", id=f"login-{a.id}", variant="primary", classes="acct-btn"),
            Button("Rename", id=f"rename-{a.id}", variant="default", classes="acct-btn"),
            Button("Remove", id=f"remove-{a.id}", variant="error", classes="acct-btn"),
            classes="acct-row",
        )


class AddAccountForm(Static):
    """Form to add a new account."""

    def compose(self) -> ComposeResult:
        yield Label("[bold]Add Account[/bold]", classes="form-title")
        yield Horizontal(
            Select(
                [(p.value.title(), p.value) for p in Provider],
                prompt="Provider",
                id="new-provider",
                classes="form-select",
            ),
            Input(
                placeholder="Label (e.g., 'Personal', 'Work')", id="new-label", classes="form-input"
            ),
            Button("Add", id="add-account", variant="success", classes="form-btn"),
            classes="form-row",
        )


class AccountsScreen(Screen):
    """Account management screen."""

    BINDINGS = [
        ("escape", "pop_screen", "Back"),
        ("b", "pop_screen", "Back"),
    ]

    CSS = """
    AccountsScreen {
        background: $surface;
    }

    #accounts-container {
        padding: 1 2;
    }

    .acct-row {
        height: 3;
        padding: 0 1;
        margin-bottom: 1;
        background: $panel;
        border: solid $primary-background;
    }

    .acct-provider {
        width: 8;
    }

    .acct-label {
        width: 16;
    }

    .acct-email {
        width: 1fr;
        color: $text-muted;
    }

    .acct-status {
        width: 14;
        text-align: center;
    }

    .acct-btn {
        min-width: 12;
        height: 1;
        margin-left: 1;
        border: none;
        text-style: bold;
    }

    .form-row {
        height: 3;
        margin: 1 0;
    }

    .form-title {
        margin: 1 0;
    }

    .form-select {
        width: 20;
    }

    .form-input {
        width: 1fr;
        margin: 0 1;
    }

    .form-btn {
        width: 10;
    }

    #rename-container {
        height: 3;
        margin: 1 0;
    }

    #rename-container Input {
        width: 1fr;
        margin-right: 1;
    }

    #rename-container Button {
        width: 12;
    }

    #login-input-container {
        margin: 1 0;
        height: auto;
    }

    #login-action-buttons {
        height: 3;
        margin: 1 0;
    }

    #login-token-row {
        height: 3;
        margin: 1 0;
    }

    #login-token-row Input {
        width: 1fr;
        margin-right: 1;
    }

    #login-token-row Button {
        width: 12;
    }

    .section-divider {
        margin: 1 0;
        color: $text-muted;
    }

    #message {
        margin: 1 0;
        height: 1;
    }
    """

    def __init__(self, account_manager: AccountManager, **kwargs):
        super().__init__(**kwargs)
        self.account_manager = account_manager
        self._login_target: str | None = None
        self._rename_target: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ScrollableContainer(
            Label("[bold]Accounts[/bold]", classes="form-title"),
            Vertical(id="account-list"),
            Label("---", classes="section-divider"),
            AddAccountForm(id="add-form"),
            Label("---", classes="section-divider"),
            Horizontal(
                Input(placeholder="New label", id="rename-input"),
                Button("Save", id="submit-rename", variant="success"),
                id="rename-container",
            ),
            Vertical(
                Label("", id="login-instructions"),
                Horizontal(
                    Button("Browser OAuth", id="browser-oauth-login", variant="success"),
                    Button("Device Flow", id="device-flow-login", variant="success"),
                    Button("Import from Codex", id="codex-import-login", variant="default"),
                    id="login-action-buttons",
                ),
                Horizontal(
                    Input(
                        placeholder="Paste token here",
                        password=True,
                        id="login-token-input",
                    ),
                    Button("Submit", id="submit-login", variant="primary"),
                    id="login-token-row",
                ),
                id="login-input-container",
            ),
            Label("", id="message"),
            id="accounts-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        # Hide login and rename containers initially
        self.query_one("#login-input-container", Vertical).display = False
        self.query_one("#rename-container", Horizontal).display = False
        self._refresh_list()

    @work(exclusive=True, group="refresh", thread=False)
    async def _refresh_list(self) -> None:
        """Refresh the account list."""
        account_list = self.query_one("#account-list", Vertical)
        await account_list.remove_children()

        accounts = self.account_manager.list_accounts()
        if not accounts:
            await account_list.mount(Label("[dim]No accounts yet. Add one below.[/dim]"))
        else:
            for account in accounts:
                await account_list.mount(AccountRow(account, id=f"row-{account.id}"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        logger.info("BUTTON PRESSED: %r (login_target=%r)", btn_id, self._login_target)
        self._show_message(f"[dim]Button: {btn_id}[/dim]")

        if btn_id == "add-account":
            self._handle_add()
        elif btn_id.startswith("login-") and not btn_id.startswith("login-token"):
            account_id = btn_id[6:]  # Remove "login-" prefix
            logger.info("Showing login input for: %s", account_id)
            self._show_login_input(account_id)
        elif btn_id.startswith("remove-"):
            account_id = btn_id[7:]
            self._handle_remove(account_id)
        elif btn_id.startswith("rename-") and btn_id != "rename-input":
            account_id = btn_id[7:]
            self._show_rename_input(account_id)
        elif btn_id == "submit-rename":
            self._handle_rename_submit()
        elif btn_id == "submit-login":
            self._handle_login_submit()
        elif btn_id == "browser-oauth-login":
            self._show_message("[dim]Starting Browser OAuth...[/dim]")
            self._handle_browser_oauth()
        elif btn_id == "device-flow-login":
            self._show_message("[dim]Starting Device Flow...[/dim]")
            self._handle_device_flow()
        elif btn_id == "codex-import-login":
            self._show_message("[dim]Importing from Codex CLI...[/dim]")
            self._handle_codex_import()
        else:
            logger.warning("Unhandled button: %r", btn_id)

    def _handle_add(self) -> None:
        """Add a new account and immediately show login prompt."""
        try:
            provider_select = self.query_one("#new-provider", Select)
            label_input = self.query_one("#new-label", Input)

            provider_value = provider_select.value
            label = label_input.value.strip()

            if provider_value is Select.BLANK:
                self._show_message("[red]Select a provider[/red]")
                return
            if not label:
                self._show_message("[red]Enter a label[/red]")
                return

            provider = Provider(provider_value)
            account = self.account_manager.add_account(provider=provider, label=label)

            # Clear form
            label_input.value = ""
            self._refresh_list()

            # Automatically show login prompt for the new account
            self._show_login_input(account.id)

        except ValueError as e:
            self._show_message(f"[red]{e}[/red]")

    def _handle_remove(self, account_id: str) -> None:
        """Remove an account."""
        if self.account_manager.remove_account(account_id):
            self._show_message(f"[green]Removed: {account_id}[/green]")
        else:
            self._show_message(f"[red]Account not found: {account_id}[/red]")
        self._refresh_list()

    def _show_rename_input(self, account_id: str) -> None:
        """Show the rename input for an account."""
        self._rename_target = account_id
        account = self.account_manager.get_account(account_id)
        container = self.query_one("#rename-container", Horizontal)
        container.display = True
        rename_input = self.query_one("#rename-input", Input)
        rename_input.value = account.label if account else ""
        rename_input.focus()
        self._show_message(f"[dim]Renaming: {account_id}[/dim]")

    def _handle_rename_submit(self) -> None:
        """Submit a rename."""
        if not self._rename_target:
            self._show_message("[red]No account selected for rename[/red]")
            return

        new_label = self.query_one("#rename-input", Input).value.strip()
        if not new_label:
            self._show_message("[red]Enter a new label[/red]")
            return

        account = self.account_manager.get_account(self._rename_target)
        if not account:
            self._show_message(f"[red]Account not found: {self._rename_target}[/red]")
            return

        account.label = new_label
        self.account_manager.update_account(account)
        self._show_message(f"[green]Renamed to: {new_label}[/green]")
        self.query_one("#rename-container", Horizontal).display = False
        self._rename_target = None
        self._refresh_list()

    def _show_login_input(self, account_id: str) -> None:
        """Show the login input for an account with provider-specific guidance."""
        self._login_target = account_id
        account = self.account_manager.get_account(account_id)
        container = self.query_one("#login-input-container", Vertical)
        container.display = True

        instructions_label = self.query_one("#login-instructions", Label)
        token_input = self.query_one("#login-token-input", Input)
        submit_btn = self.query_one("#submit-login", Button)
        browser_btn = self.query_one("#browser-oauth-login", Button)
        device_btn = self.query_one("#device-flow-login", Button)
        codex_btn = self.query_one("#codex-import-login", Button)
        action_row = self.query_one("#login-action-buttons", Horizontal)
        token_row = self.query_one("#login-token-row", Horizontal)

        # Hide everything first, then show what's relevant
        token_input.display = False
        submit_btn.display = False
        browser_btn.display = False
        device_btn.display = False
        codex_btn.display = False
        action_row.display = False
        token_row.display = False

        if account and account.provider == Provider.CLAUDE:
            instructions_label.update(
                f"[bold]Login: {account.label}[/bold] (Claude)\n"
                "Click [bold]Browser OAuth[/bold] to open Claude login in your browser.\n"
                "This is the recommended method — no tokens to copy."
            )
            action_row.display = True
            browser_btn.display = True
            browser_btn.label = "Browser OAuth"

        elif account and account.provider == Provider.COPILOT:
            instructions_label.update(
                f"[bold]Login: {account.label}[/bold] (Copilot)\n"
                "Click [bold]Device Flow[/bold] to get a code, "
                "then authorize at github.com.\n"
                "Or paste a GitHub token (gho_...) below."
            )
            action_row.display = True
            device_btn.display = True
            token_row.display = True
            token_input.display = True
            token_input.placeholder = "GitHub token (gho_...)"
            submit_btn.display = True

        elif account and account.provider == Provider.CHATGPT:
            instructions_label.update(
                f"[bold]Login: {account.label}[/bold] (ChatGPT)\n"
                "Click [bold]Browser OAuth[/bold] to login via OpenAI (recommended).\n"
                "[bold]Device Flow[/bold] for headless/SSH. "
                "[bold]Import from Codex[/bold] if you have Codex CLI tokens.\n"
                "Or paste a session token below as last resort."
            )
            action_row.display = True
            browser_btn.display = True
            browser_btn.label = "Browser OAuth"
            device_btn.display = True
            codex_btn.display = True
            token_row.display = True
            token_input.display = True
            token_input.placeholder = "Session token (fallback)"
            submit_btn.display = True

        else:
            instructions_label.update(f"[bold]Login: {account_id}[/bold]\nPaste a token below.")
            token_row.display = True
            token_input.display = True
            submit_btn.display = True

        self._show_message("")

    def _handle_login_submit(self) -> None:
        """Submit a token for login."""
        if not self._login_target:
            self._show_message("[red]No account selected for login[/red]")
            return

        token = self.query_one("#login-token-input", Input).value.strip()
        if not token:
            self._show_message("[red]Enter a token[/red]")
            return

        self._do_token_login(self._login_target, token)

    @work(exclusive=True, group="auth", thread=False)
    async def _do_token_login(self, account_id: str, token: str) -> None:
        """Perform token-based login."""
        self._show_message("Authenticating...")
        try:
            account = self.account_manager.get_account(account_id)
            if not account:
                self._show_message(f"[red]Account not found: {account_id}[/red]")
                return

            if account.provider == Provider.COPILOT:
                await self.account_manager.login_with_token(account_id, token)
            elif account.provider == Provider.CLAUDE:
                # Try as session key first, then as OAuth token
                try:
                    await self.account_manager.login_with_session_key(account_id, token)
                except Exception:
                    await self.account_manager.login_with_token(account_id, token)
            elif account.provider == Provider.CHATGPT:
                await self.account_manager.login_with_session_key(account_id, token)

            self._show_message(f"[green]Logged in: {account_id}[/green]")
            self.query_one("#login-token-input", Input).value = ""
            self._refresh_list()

        except Exception as e:
            self._show_message(f"[red]Login failed: {e}[/red]")

    @work(exclusive=True, group="auth", thread=False)
    async def _handle_browser_oauth(self) -> None:
        """Start OAuth browser flow for Claude or ChatGPT."""
        if not self._login_target:
            self._show_message("[red]No account selected — click Login first[/red]")
            return

        account = self.account_manager.get_account(self._login_target)
        if not account or account.provider not in (Provider.CLAUDE, Provider.CHATGPT):
            self._show_message(
                "[red]Browser OAuth only works for Claude and ChatGPT accounts[/red]"
            )
            return

        provider_name = account.provider.value.title()
        self._show_message(f"Opening browser for {provider_name} OAuth login...")

        def on_url(url: str):
            self._show_message(
                f"Waiting for browser authorization... [dim](URL: {url[:60]}...)[/dim]"
            )
            self.notify(
                "Waiting for browser authorization...",
                title=f"{provider_name} OAuth",
                timeout=120,
            )

        try:
            if account.provider == Provider.CLAUDE:
                await self.account_manager.login_claude_browser(self._login_target, on_url=on_url)
            else:
                await self.account_manager.login_chatgpt_browser(self._login_target, on_url=on_url)
            self._show_message(f"[green]Logged in: {self._login_target}[/green]")
            self._refresh_list()
        except Exception as e:
            self._show_message(f"[red]Browser OAuth failed: {e}[/red]")

    @work(exclusive=True, group="auth", thread=False)
    async def _handle_device_flow(self) -> None:
        """Start device flow for Copilot or ChatGPT."""
        logger.info("_handle_device_flow ENTERED, login_target=%r", self._login_target)
        if not self._login_target:
            self._show_message("[red]No account selected — click Login first[/red]")
            return

        account = self.account_manager.get_account(self._login_target)
        if not account or account.provider not in (Provider.COPILOT, Provider.CHATGPT):
            self._show_message("[red]Device flow only works for Copilot and ChatGPT accounts[/red]")
            return

        provider_name = account.provider.value.title()
        self._show_message(f"Starting {provider_name} device flow...")

        def on_code(uri: str, code: str):
            self._show_message(
                f"Go to [bold link={uri}]{uri}[/bold link] and enter code: [bold]{code}[/bold]"
            )
            self.notify(
                f"Enter code [bold]{code}[/bold] at {uri}",
                title=f"{provider_name} Device Flow",
                timeout=120,
            )
            import webbrowser

            webbrowser.open(uri)

        try:
            if account.provider == Provider.COPILOT:
                await self.account_manager.login_copilot_device_flow(
                    self._login_target, on_user_code=on_code
                )
            else:
                await self.account_manager.login_chatgpt_device_flow(
                    self._login_target, on_user_code=on_code
                )
            self._show_message(f"[green]Logged in: {self._login_target}[/green]")
            self._refresh_list()
        except Exception as e:
            self._show_message(f"[red]Device flow failed: {e}[/red]")

    @work(exclusive=True, group="auth", thread=False)
    async def _handle_codex_import(self) -> None:
        """Import ChatGPT credentials from Codex CLI."""
        if not self._login_target:
            self._show_message("[red]No account selected — click Login first[/red]")
            return

        account = self.account_manager.get_account(self._login_target)
        if not account or account.provider != Provider.CHATGPT:
            self._show_message("[red]Codex import only works for ChatGPT accounts[/red]")
            return

        self._show_message("Importing tokens from Codex CLI...")

        try:
            await self.account_manager.login_chatgpt_codex_import(self._login_target)
            self._show_message(f"[green]Logged in: {self._login_target}[/green]")
            self.notify("ChatGPT authenticated via Codex CLI", title="Success", timeout=5)
            self._refresh_list()
        except Exception as e:
            self._show_message(f"[red]Codex import failed: {e}[/red]")

    def _show_message(self, text: str) -> None:
        """Show a message in the status area."""
        try:
            self.query_one("#message", Label).update(text)
            logger.info("Message updated: %s", text[:80])
        except Exception as e:
            logger.error("Failed to update message: %s", e)

    def action_pop_screen(self) -> None:
        """Go back to dashboard."""
        self.app.pop_screen()
