"""Usage card widget — displays usage data for a single account."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Label, ProgressBar, Static

from ai_usage.domain.models import Provider, Quota, UsageData


def _usage_color(percent: float) -> str:
    """Color based on usage percentage."""
    if percent < 50:
        return "green"
    elif percent < 75:
        return "yellow"
    elif percent < 90:
        return "dark_orange"
    return "red"


def _provider_icon(provider: Provider) -> str:
    """Text icon for each provider."""
    return {
        Provider.CLAUDE: "CL",
        Provider.CHATGPT: "GP",
        Provider.COPILOT: "GH",
    }.get(provider, "??")


class QuotaBar(Static):
    """A single quota row — info line + thin progress bar."""

    def __init__(self, quota: Quota, **kwargs) -> None:
        super().__init__(**kwargs)
        self.quota = quota

    def compose(self) -> ComposeResult:
        q = self.quota
        pct = q.usage_percent
        color = _usage_color(pct)

        # Info line: "  Name  42/300 unit  (resets Xh)  14%"
        label_parts = [f"  {q.name.replace('_', ' ').title()}"]
        if q.limit is not None:
            label_parts.append(f"{q.used:.0f}/{q.limit:.0f} {q.unit}")
        elif q.remaining is not None:
            label_parts.append(f"{q.remaining:.0f} remaining")
        else:
            label_parts.append(f"{q.used:.0f} {q.unit}")

        reset_str = q.reset_in_human
        if reset_str:
            label_parts.append(f"(resets {reset_str})")

        pct_str = f"[{color}]{pct:.0f}%[/{color}]"
        yield Horizontal(
            Label(f"{' '.join(label_parts)}", classes="quota-label"),
            Label(pct_str, classes="quota-pct"),
            classes="quota-info",
        )
        yield ProgressBar(total=100, show_percentage=False, show_eta=False, classes="quota-bar")

    def on_mount(self) -> None:
        bar = self.query_one(ProgressBar)
        bar.update(progress=self.quota.usage_percent)


class UsageCard(Static):
    """Card showing usage data for one account."""

    usage_data: reactive[UsageData | None] = reactive(None)

    def __init__(self, usage_data: UsageData | None = None, account_label: str = "", **kwargs):
        super().__init__(**kwargs)
        self._account_label = account_label
        self.usage_data = usage_data

    def compose(self) -> ComposeResult:
        data = self.usage_data

        if data is None:
            yield Label("Loading...", classes="card-loading")
            return

        provider_icon = _provider_icon(data.provider)
        plan_str = data.plan_name or "Unknown Plan"

        # Header
        yield Horizontal(
            Label(
                f"[bold]{provider_icon}[/bold] {self._account_label}",
                classes="card-title",
            ),
            Label(f"{plan_str}", classes="card-plan"),
            classes="card-header",
        )

        if data.is_error:
            yield Label(f"[red]Error: {data.error}[/red]", classes="card-error")
            return

        # Quotas
        if data.quotas:
            for quota in data.quotas:
                yield QuotaBar(quota, classes="quota-row")
        else:
            yield Label("  No usage data available", classes="card-empty")

        # Model breakdown
        if data.model_breakdown:
            parts = []
            for m in data.model_breakdown:
                parts.append(f"{m.model_name}: {m.usage_percent:.0f}%")
            yield Label(f"  Models: {' | '.join(parts)}", classes="card-models")

    def update_data(self, data: UsageData) -> None:
        """Update the card with new data, triggering a re-render."""
        self.usage_data = data
        self._account_label = self._account_label  # keep existing label
        # Force re-compose
        self.remove_children()
        for widget in self.compose():
            self.mount(widget)
