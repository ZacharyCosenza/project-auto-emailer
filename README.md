# NYC Weekend Auto-Emailer

Automated weekly email digest of NYC events, weather, and transit. Every Friday morning it searches the web for this weekend's events, summarizes each section with Google Gemini, and emails you the result.

## What It Does

Each run:

1. **Searches** the web for each configured topic (Serper → Brave → DuckDuckGo fallback)
2. **Scrapes** each result page for full content (trafilatura → Jina Reader fallback for JS-heavy sites)
3. **Filters** event results to only include content with matching weekend dates
4. **Summarizes** each section with Google Gemini 2.5 Flash
5. **Emails** the formatted digest via Gmail SMTP

## Quick Start

```bash
# 1. Install
pip install -e .

# 2. Set API keys
export GEMINI_API_KEY="your-key"           # from aistudio.google.com/apikey
export SERPER_API_KEY="your-key"           # from serper.dev (optional but recommended)
export AUTO_EMAILER_SMTP_PASSWORD="..."    # Gmail app password

# 3. Test (no email sent, no API keys needed)
python -m auto_emailer --config config.json run --dry-run

# 4. Send for real
python -m auto_emailer --config config.json run

# 5. Install as a recurring systemd timer (runs every Friday at 8am)
python -m auto_emailer --config config.json install

# 6. Check API usage this month
python -m auto_emailer usage
```

Note: `--config` must come before the subcommand.

## CLI Commands

| Command | Description |
|---------|-------------|
| `run` | Run once: search, summarize, email |
| `run --dry-run` | Print email to stdout instead of sending (no API keys needed) |
| `start` | Start scheduler daemon in the foreground |
| `install` | Install as a systemd user timer (runs on schedule, survives reboots) |
| `usage [--month YYYY-MM]` | Show API usage for the current (or specified) month |

## Configuration

Edit `config.json`. Each entry in `searches` is one section of the email:

```json
{
  "instructions": ["Exclude children's and family-only events."],
  "searches": [
    {
      "query": "classical music symphony orchestra ballet opera this weekend NYC",
      "label": "Music & Dance",
      "type": "events",
      "max_events": 3,
      "sources": ["https://new-york.events/concerts/classical/"],
      "instructions": ["Exclude flamenco and folk music."]
    },
    {
      "query": "NYC weather forecast this weekend",
      "label": "Weather",
      "type": "summary"
    }
  ],
  "email": { "smtp_host": "smtp.gmail.com", "smtp_port": 587, "sender": "you@gmail.com", "recipients": ["you@gmail.com"], "subject_prefix": "NYC Weekend" },
  "llm": { "model": "gemini-2.5-flash", "max_output_tokens": 16384, "temperature": 0, "call_delay": 20 },
  "search": { "max_results": 8 },
  "scrape": { "enabled": true, "timeout": 10, "max_chars_per_page": 20000 },
  "schedule": { "cron": "0 8 * * 5" }
}
```

### Search entry fields

| Field | Required | Description |
|-------|----------|-------------|
| `query` | yes | Search query |
| `type` | no | `"events"` (default) or `"summary"`. Events applies date filtering; summary is for weather/transit/news |
| `label` | no | Section header in the email. Derived from query if omitted |
| `max_events` | no | Cap the LLM to at most N events in its output |
| `sources` | no | Extra URLs always fetched regardless of search results (guaranteed seeds) |
| `instructions` | no | Per-section prompt rules appended to the base rules |

Top-level `instructions` apply to every section and are merged with per-section instructions.

### Key config settings

| Key | Default | Description |
|-----|---------|-------------|
| `llm.call_delay` | `5` | Seconds between Gemini calls (raise if you hit 429s) |
| `search.max_results` | `5` | Serper results per query |
| `scrape.max_chars_per_page` | `20000` | Max extracted content per page |
| `schedule.cron` | `0 8 * * 5` | When to run (5-part cron: Fridays at 8am) |

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | From [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (free) |
| `AUTO_EMAILER_SMTP_PASSWORD` | Yes | Gmail [app password](https://myaccount.google.com/apppasswords) |
| `SERPER_API_KEY` | No | From [serper.dev](https://serper.dev) (2,500 free/month). Falls back to Brave → DDG |
| `BRAVE_SEARCH_API_KEY` | No | From Brave Search API (1,000 free/month). Used if Serper unavailable |

## Automatic Scheduling

Run `install` once to register a systemd user timer on your desktop Linux machine:

```bash
source keys.md
python -m auto_emailer --config config.json install
```

This creates `~/.config/systemd/user/auto-emailer.{service,timer}` and enables them. The timer fires every Friday at 8am (configurable via `schedule.cron`). With `Persistent=true`, if your machine was asleep at 8am Friday it will run automatically on next wake.

Check the timer:
```bash
systemctl --user status auto-emailer.timer
systemctl --user list-timers auto-emailer.timer
journalctl --user -u auto-emailer -n 50
```

## API Usage Monitoring

Every API call is logged to `~/.local/share/auto-emailer/usage.jsonl`. View this month's usage:

```bash
python -m auto_emailer usage
# API Usage — March 2026
# ──────────────────────────────────
# Gemini   6 calls   tokens in: 120,000   tokens out: 3,000
# Serper   6 searches
# Jina     20 fetches

python -m auto_emailer usage --month 2026-02
```

## Gemini API Rate Limits (Free Tier)

| Model | RPM | RPD |
|-------|-----|-----|
| `gemini-2.5-flash` | 15 | 1500 |

Each run makes **1 call per search entry** (6 calls with default config). Well within free tier for weekly runs. If you test repeatedly in one day, you may exhaust the daily quota. Raise `llm.call_delay` to avoid per-minute rate limits.

## Development

```bash
# Dry run — prints email to stdout, writes debug files
python -m auto_emailer --config config.json run --dry-run

# Debug output
cat search_debug.txt    # per-section stats: sources, dates found, filter metrics
cat search_extract.txt  # exact prompts sent to Gemini + LLM outputs
cat email_preview.txt   # the email as it would have been sent
```

---

**Made with Claude Code** | [Technical Docs](CLAUDE.md)
