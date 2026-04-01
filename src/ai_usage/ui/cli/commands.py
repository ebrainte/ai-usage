"""CLI commands using Typer.

Provides both a TUI dashboard and quick CLI commands:
  ai-usage             — Launch TUI dashboard
  ai-usage check       — Quick table view of all usage
  ai-usage accounts    — Manage accounts
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from ai_usage.app.account_manager import AccountManager
from ai_usage.app.usage_service import UsageService
from ai_usage.domain.models import Provider

cli = typer.Typer(help="AI Usage Tracker — track Claude, ChatGPT, and Copilot usage")
accounts_cli = typer.Typer(help="Manage provider accounts")
cli.add_typer(accounts_cli, name="accounts")

console = Console()


@cli.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    refresh: int = typer.Option(
        300, "--refresh", "-r", help="Auto-refresh interval in seconds (0 to disable)"
    ),
):
    """Launch the AI Usage Tracker TUI dashboard."""
    if ctx.invoked_subcommand is not None:
        return

    from ai_usage.ui.tui.app import AiUsageApp

    app = AiUsageApp(auto_refresh_seconds=refresh)
    app.run()


@cli.command()
def check(
    provider: str = typer.Option(None, "--provider", "-p", help="Filter by provider"),
):
    """Quick check — show usage for all accounts in a table."""
    manager = AccountManager()
    service = UsageService(account_manager=manager)

    accounts = manager.list_accounts()
    if not accounts:
        console.print("[dim]No accounts configured.[/dim]")
        console.print(
            "Add accounts with: [bold]ai-usage accounts add --provider claude --label Personal[/bold]"
        )
        return

    # Filter by provider if specified
    if provider:
        try:
            p = Provider(provider.lower())
            accounts = [a for a in accounts if a.provider == p]
        except ValueError:
            console.print(f"[red]Unknown provider: {provider}[/red]")
            console.print(f"Available: {', '.join(p.value for p in Provider)}")
            return

    # Fetch usage
    console.print("[dim]Fetching usage data...[/dim]")
    results = asyncio.run(service.fetch_all())
    results_map = {r.account_id: r for r in results}

    # Build table
    table = Table(title="AI Usage", show_lines=True)
    table.add_column("Provider", style="bold")
    table.add_column("Account")
    table.add_column("Plan")
    table.add_column("Usage", justify="right")
    table.add_column("Details")
    table.add_column("Status")

    for account in accounts:
        data = results_map.get(account.id)
        if not data:
            table.add_row(
                account.provider.value.upper(),
                account.display_name,
                "-",
                "-",
                "-",
                "[dim]no data[/dim]",
            )
            continue

        if data.is_error:
            # Truncate long error messages for the table
            error_text = data.error or "Unknown error"
            if len(error_text) > 60:
                error_text = error_text[:57] + "..."
            table.add_row(
                account.provider.value.upper(),
                account.display_name,
                data.plan_name or "-",
                "-",
                f"[red]{error_text}[/red]",
                "[red]error[/red]",
            )
            continue

        # Build usage string from primary quota
        primary = data.primary_quota
        usage_str = "-"
        detail_parts = []

        if primary:
            pct = primary.usage_percent
            color = "green" if pct < 50 else "yellow" if pct < 75 else "red"
            usage_str = f"[{color}]{pct:.0f}%[/{color}]"

            if primary.reset_in_human:
                detail_parts.append(f"resets in {primary.reset_in_human}")

        # Add other quotas as details
        for q in data.quotas:
            if q != primary:
                detail_parts.append(f"{q.name}: {q.usage_percent:.0f}%")

        # Model breakdown
        for m in data.model_breakdown:
            detail_parts.append(f"{m.model_name}: {m.usage_percent:.0f}%")

        table.add_row(
            account.provider.value.upper(),
            account.display_name,
            data.plan_name or "-",
            usage_str,
            " | ".join(detail_parts) if detail_parts else "-",
            "[green]ok[/green]",
        )

    console.print(table)


# --- Account management commands ---


@accounts_cli.command("list")
def accounts_list():
    """List all configured accounts."""
    manager = AccountManager()
    accounts = manager.list_accounts()

    if not accounts:
        console.print("[dim]No accounts configured.[/dim]")
        return

    table = Table(title="Accounts")
    table.add_column("ID", style="bold")
    table.add_column("Provider")
    table.add_column("Label")
    table.add_column("Email")
    table.add_column("Status")
    table.add_column("Auth Method")

    for a in accounts:
        status_color = {
            "active": "green",
            "auth_expired": "yellow",
            "error": "red",
            "unconfigured": "dim",
        }.get(a.status, "dim")

        table.add_row(
            a.id,
            a.provider.value,
            a.label,
            a.email or "-",
            f"[{status_color}]{a.status.value}[/{status_color}]",
            a.credential.auth_method.value if a.credential else "-",
        )

    console.print(table)


@accounts_cli.command("add")
def accounts_add(
    provider: str = typer.Option(
        ..., "--provider", "-p", help="Provider: claude, chatgpt, copilot"
    ),
    label: str = typer.Option(..., "--label", "-l", help="Human-friendly label"),
    account_id: str = typer.Option(None, "--id", help="Custom account ID"),
):
    """Add a new account."""
    try:
        p = Provider(provider.lower())
    except ValueError:
        console.print(f"[red]Unknown provider: {provider}[/red]")
        console.print(f"Available: {', '.join(p.value for p in Provider)}")
        return

    manager = AccountManager()
    try:
        account = manager.add_account(provider=p, label=label, account_id=account_id)
        console.print(f"[green]Added account: {account.id}[/green]")
        console.print(f"Now login with: [bold]ai-usage accounts login {account.id}[/bold]")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")


@accounts_cli.command("remove")
def accounts_remove(
    account_id: str = typer.Argument(help="Account ID to remove"),
):
    """Remove an account."""
    manager = AccountManager()
    if manager.remove_account(account_id):
        console.print(f"[green]Removed: {account_id}[/green]")
    else:
        console.print(f"[red]Account not found: {account_id}[/red]")


@accounts_cli.command("login")
def accounts_login(
    account_id: str = typer.Argument(help="Account ID to login"),
    token: str = typer.Option(None, "--token", "-t", help="Session key or token"),
    device_flow: bool = typer.Option(
        False, "--device-flow", "-d", help="Use GitHub device flow (Copilot)"
    ),
    browser: bool = typer.Option(False, "--browser", "-b", help="Use OAuth browser flow (Claude)"),
    import_cli: bool = typer.Option(
        False, "--import-cli", help="Import credentials from existing CLI tools"
    ),
):
    """Login to an account."""
    manager = AccountManager()
    account = manager.get_account(account_id)

    if not account:
        console.print(f"[red]Account not found: {account_id}[/red]")
        return

    if import_cli:
        # Try to import from CLI tools
        console.print(
            f"[dim]Importing credentials from CLI tools for {account.provider.value}...[/dim]"
        )
        try:
            result = asyncio.run(manager.login(account_id))
            console.print(f"[green]Imported credentials for: {result.display_name}[/green]")
            return
        except Exception as e:
            console.print(f"[red]Import failed: {e}[/red]")
            return

    if browser:
        if account.provider != Provider.CLAUDE:
            console.print("[red]Browser OAuth only supported for Claude[/red]")
            return

        console.print("[dim]Opening browser for Claude OAuth login...[/dim]")

        def on_url(url: str):
            console.print(
                f"If the browser doesn't open, visit:\n[bold link={url}]{url}[/bold link]\n"
            )
            console.print("[dim]Waiting for authorization...[/dim]")

        try:
            result = asyncio.run(manager.login_claude_browser(account_id, on_url=on_url))
            console.print(f"[green]Logged in: {result.display_name}[/green]")
        except Exception as e:
            console.print(f"[red]Browser login failed: {e}[/red]")
        return

    if device_flow:
        if account.provider != Provider.COPILOT:
            console.print("[red]Device flow only supported for Copilot[/red]")
            return

        def on_code(uri: str, code: str):
            console.print(f"\nGo to [bold link={uri}]{uri}[/bold link]")
            console.print(f"Enter code: [bold green]{code}[/bold green]\n")
            console.print("[dim]Waiting for authorization...[/dim]")

        try:
            result = asyncio.run(
                manager.login_copilot_device_flow(account_id, on_user_code=on_code)
            )
            console.print(f"[green]Logged in: {result.display_name}[/green]")
        except Exception as e:
            console.print(f"[red]Device flow failed: {e}[/red]")
        return

    if token:
        _do_token_login(manager, account_id, account, token)
        return

    # Interactive: suggest the right login method
    if account.provider == Provider.COPILOT:
        console.print(
            "For Copilot, use [bold]--device-flow[/bold] or provide a [bold]--token[/bold]"
        )
        console.print(f"  ai-usage accounts login {account_id} --device-flow")
        return

    if account.provider == Provider.CLAUDE:
        console.print(
            "For Claude, use [bold]--browser[/bold] for OAuth or provide a [bold]--token[/bold]"
        )
        console.print(f"  ai-usage accounts login {account_id} --browser")
        return

    token = typer.prompt(
        "Enter session key / token",
        hide_input=True,
    )
    _do_token_login(manager, account_id, account, token)


def _do_token_login(manager: AccountManager, account_id: str, account, token: str):
    """Perform token-based login."""
    try:
        if account.provider == Provider.COPILOT:
            result = asyncio.run(manager.login_with_token(account_id, token))
        elif account.provider == Provider.CLAUDE:
            try:
                result = asyncio.run(manager.login_with_session_key(account_id, token))
            except Exception:
                result = asyncio.run(manager.login_with_token(account_id, token))
        elif account.provider == Provider.CHATGPT:
            result = asyncio.run(manager.login_with_session_key(account_id, token))
        else:
            console.print(f"[red]Unsupported provider: {account.provider}[/red]")
            return
        console.print(f"[green]Logged in: {result.display_name}[/green]")
    except Exception as e:
        console.print(f"[red]Login failed: {e}[/red]")


@accounts_cli.command("validate")
def accounts_validate():
    """Validate credentials for all accounts."""
    manager = AccountManager()
    results = asyncio.run(manager.validate_all())

    if not results:
        console.print("[dim]No accounts to validate.[/dim]")
        return

    for account_id, is_valid in results.items():
        status = "[green]valid[/green]" if is_valid else "[red]invalid[/red]"
        console.print(f"  {account_id}: {status}")
