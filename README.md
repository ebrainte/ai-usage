# ai-usage

Track your AI subscription usage across **Claude**, **ChatGPT**, and **GitHub Copilot** — all in one terminal dashboard. Supports multiple accounts per provider.

```
 CL Personal                              Pro Plan
   Session    234/500 requests  (resets 2h 14m)   47%
   ████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░

 GH Work                             Business Plan
   Completions    1420/4000 requests  (resets 3d)  36%
   ███████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
   Premium        12/300 requests     (resets 28d)  4%
   ██░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
```

## Features

- **Multi-account** — Track multiple Claude accounts, multiple Copilot accounts, etc.
- **Independent auth** — Own login flows per provider; not tied to CLI tools on your machine
- **Live dashboard** — Auto-refreshing TUI with progress bars and color-coded usage
- **CLI mode** — One-shot `ai-usage check` for quick status in your terminal
- **Secure credentials** — Tokens stored in macOS Keychain via `keyring`, never in plain files
- **Provider-specific login UX** — OAuth browser flow (Claude, ChatGPT), device flow (Copilot, ChatGPT), Codex CLI import, manual tokens

## Supported Providers

| Provider | Auth Methods | Usage Data |
|----------|-------------|------------|
| **Claude** | OAuth browser flow, token import | Session & weekly limits, extra usage credits |
| **GitHub Copilot** | Device flow, PAT, CLI import | Completions, chat, premium interactions |
| **ChatGPT** | OAuth browser flow, device flow, Codex CLI import | 5-hour & weekly rate limits, credits |

## Install

### Homebrew (recommended)

```bash
brew install ebrainte/tap/ai-usage
```

### With pipx

```bash
pipx install .
```

### With uv

```bash
uv tool install .
```

### From source (development)

```bash
uv sync
uv run ai-usage
```

## Quick Start

```bash
# 1. Add an account
ai-usage accounts add --provider claude --label "Personal"

# 2. Login (opens browser for OAuth)
ai-usage accounts login claude-personal --browser

# 3. Launch the dashboard
ai-usage
```

## Usage

### TUI Dashboard (default)

```bash
ai-usage                    # Launch dashboard
ai-usage --refresh 60       # Auto-refresh every 60s (default: 300s)
```

**Keybindings:**

| Key | Action |
|-----|--------|
| `r` | Refresh usage data |
| `t` | Cycle auto-refresh interval (1m / 5m / 10m / 1h) |
| `a` | Manage accounts |
| `q` | Quit |

### CLI One-Shot

```bash
ai-usage check              # Table view of all accounts
ai-usage check -p claude    # Filter by provider
```

### Account Management

```bash
ai-usage accounts list                                          # List all accounts
ai-usage accounts add --provider claude --label "Work"          # Add account
ai-usage accounts add --provider copilot --label "Personal"     # Add Copilot account
ai-usage accounts add --provider chatgpt --label "Personal"     # Add ChatGPT account
ai-usage accounts login <account-id> --browser                  # Claude/ChatGPT OAuth (browser)
ai-usage accounts login <account-id> --device-flow              # Copilot/ChatGPT device flow
ai-usage accounts login <account-id> --import-codex             # ChatGPT import from Codex CLI
ai-usage accounts login <account-id> --token "gho_..."          # Manual token
ai-usage accounts remove <account-id>                           # Remove account
ai-usage accounts validate                                      # Check all credentials
```

## Architecture

Hexagonal architecture (ports & adapters) — the domain knows nothing about HTTP, TUI, or storage.

```
src/ai_usage/
  domain/          # Models, events, exceptions — zero dependencies
    models.py      # Account, UsageData, Quota, Provider, Credential
    events.py      # Domain events
    exceptions.py  # AuthenticationError, FetchError, ConfigError
  ports/           # Interfaces (protocols)
    auth.py        # AuthPort
    usage.py       # UsagePort
    storage.py     # StoragePort
  adapters/        # Implementations
    claude/        # OAuth PKCE + usage fetcher
    copilot/       # Device flow + internal GitHub API
    chatgpt/       # OAuth PKCE + device flow + wham/usage API
    storage/       # YAML config + macOS Keychain (keyring)
  app/             # Application services
    account_manager.py   # Account CRUD + login orchestration
    usage_service.py     # Parallel async fetch + caching
  ui/
    tui/           # Textual dashboard + account management
    cli/           # Typer commands
```

## Config

All configuration lives in `~/.config/ai-usage/`:

| File | Purpose |
|------|---------|
| `accounts.yaml` | Account definitions (provider, label, credential metadata) |
| `debug.log` | Debug logs |

Secrets (OAuth tokens, PATs) are stored in **macOS Keychain** — never in config files.

## Requirements

- Python >= 3.12
- macOS (for Keychain credential storage)

## License

MIT
