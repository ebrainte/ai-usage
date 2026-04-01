"""Main entry point for ai-usage.

Dispatches to either the TUI dashboard or CLI commands.
"""

import logging
from pathlib import Path

from ai_usage.ui.cli.commands import cli

# Set up file-based logging for debugging
_log_path = Path.home() / ".config" / "ai-usage" / "debug.log"
_log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_log_path),
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# Typer app is the entry point — it either launches the TUI (default)
# or runs a CLI subcommand.
app = cli

if __name__ == "__main__":
    app()
