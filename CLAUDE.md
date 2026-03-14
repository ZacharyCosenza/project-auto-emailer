# Auto-Emailer — Technical Reference

Weekly NYC events email: web search → scrape → LLM summarize → Gmail SMTP.

## Tech Stack

- Python 3.10+
- `google-genai` — Gemini API (2.5 Flash)
- `trafilatura` — web page content extraction (primary)
- `requests` — Jina Reader API fallback + Serper/Brave search
- `ddgs` — DuckDuckGo search (fallback when no search API key)
- `apscheduler` — cron-based scheduler daemon
- `smtplib` (stdlib) — SMTP email delivery

## Project Structure

```
src/auto_emailer/
  __init__.py      # Package version
  __main__.py      # Entry point for `python -m auto_emailer`
  cli.py           # CLI argument parsing and command dispatch
  config.py        # JSON config loading, validation, env var injection
  core.py          # Main pipeline: search → filter → summarize → format → send
  search.py        # Web search (Serper/Brave/DDG), page fetching, date extraction
  llm.py           # Gemini client, text generation, repetition dedup, retry
  email.py         # SMTP email composition and delivery with retry
  scheduler.py     # APScheduler daemon (used by `start` command)
  installer.py     # systemd user timer installation (used by `install` command)
  usage.py         # JSONL usage log + monthly report (used by `usage` command)

config.json          # Active config (DO NOT commit — contains personal email)
keys.md              # `export VAR=...` lines for API keys (DO NOT commit)
```

## Architecture

Purely functional — no classes. Pipeline flow:

```
CLI (cli.py)
  → load_config()        # validates JSON structure + env vars
  → core.run()
      for each search entry in config["searches"]:
        if type == "events":
          → search_events()
              → web_search()             # Serper → Brave → DDG
              → fetch explicit sources   # sequentially, with retry
              → ThreadPoolExecutor: fetch_page_content() per result
              → two-pass date filter     # keep only weekend-matching results
              → generate_response()      # Gemini LLM
        if type == "summary":
          → search_summary()
              → web_search()
              → ThreadPoolExecutor: fetch_page_content() per result
              → generate_response()
        [call_delay between LLM calls]
      → format_email()
      → send_email() (or print if --dry-run)
```

## Config Format

The `searches` array drives the run. Each entry can be a plain string (treated as an `events` query) or a dict:

```json
{
  "instructions": ["Global rule applied to every section"],
  "searches": [
    {
      "query": "...",
      "type": "events",       // or "summary" — controls date filtering + prompt style
      "label": "...",         // section header in email (derived from query if omitted)
      "max_events": 3,        // LLM cap: at most N events in output
      "sources": ["https://..."],  // extra URLs fetched regardless of search results
      "instructions": ["Per-section rule"]
    }
  ]
}
```

Global `instructions` + per-section `instructions` are merged and appended to the base prompt rules.

## Web Search

`web_search()` in `search.py` tries in order:
1. **Serper** (Google Search API, `SERPER_API_KEY` — 2,500 free/month)
2. **Brave** (Brave Search API, `BRAVE_SEARCH_API_KEY` — 1,000 free/month)
3. **DuckDuckGo** (no key needed, rate-limited)

The query has `"this weekend"` stripped and replaced with the concrete Friday date (e.g. `"March 20 2026"`) to surface current listings rather than general guides.

Blocked domains (`BLOCKED_DOMAINS` in `search.py`) are filtered from all results. Preferred domains (`PREFERRED_DOMAINS`) are ranked higher in the two-pass filter.

## Page Fetching

`fetch_page_content()` tries:
1. **trafilatura** — fast static HTML extraction, no external API
2. **Jina Reader** (`r.jina.ai/{url}`) — server-side JS rendering fallback

If trafilatura returns ≥75% of `max_chars`, its result is used directly. Otherwise Jina is tried and the longer result wins. Serper result snippets are prepended to fetched content — snippets often contain event dates that JS-rendered pages hide from scrapers.

Explicit `sources` from config are fetched sequentially with retry (up to 3 attempts) before the parallel Serper pool, to ensure reliable seeding of curated listing pages.

## Date Filtering (events only)

`extract_dates()` extracts all date mentions from text using four regex patterns:
- Day-of-week + month: `"Sun Feb 22"`, `"Friday, March 20"`
- Named: `"February 20"`, `"Feb 20, 2026"`
- Numeric: `"2/20/2026"`
- Day-number lists: `"FEB 20, 21, 22"` (common in performing arts listings)

**Two-pass filter** in `search_events()`:
1. Partition all results into `weekend_results` (any date matching Fri-Sun) vs `other_results`
2. Build the LLM prompt using ONLY `weekend_results` (top 5 by domain preference + content length)

Three prompt cases:
1. Weekend dates found → bulleted event list, strong "confirmed dates" prompt
2. Specific dates found but none match → tell LLM to state no events this weekend
3. No specific dates → hedged "might be happening" prose

## LLM

`generate_response()` in `llm.py`:
- Gemini 2.5 Flash (configurable via `llm.model`)
- Temperature 0, deterministic output
- Retries up to 3× on 429/RESOURCE_EXHAUSTED with 45s/90s/135s backoff
- Post-processes output with `_truncate_repetition()` to remove degenerate loops
- Logs each call to `~/.local/share/auto-emailer/usage.jsonl` (see usage tracking)

All prompts include `Today is {date}` for temporal anchoring.

**Free tier limits (Flash 2.5):** 15 RPM, 1500 RPD. Default config runs 6 sections = 6 calls. Raise `llm.call_delay` (default 20s) if you hit 429 errors.

## Email Formatting

`format_email()` in `core.py`:
- Uses `labels[query]` if provided, else auto-derives from query string
- Inline event links: `• **Title** - Day, Time - Venue [→](URL)`
- Falls back to source link list only if LLM summary is short (<200 chars)

## Automatic Scheduling (`install` command)

`installer.py` creates a systemd user timer pair:

- `~/.config/systemd/user/auto-emailer.service` — runs `python -m auto_emailer run` as a one-shot
- `~/.config/systemd/user/auto-emailer.timer` — fires on `OnCalendar` derived from `schedule.cron`, with `Persistent=true`
- `~/.config/auto-emailer/env` — env file with API keys (chmod 600)

`Persistent=true` means: if the machine was asleep at the scheduled time, systemd runs the job immediately on next wake. No missed Friday emails.

Cron parsing: only supports `M H * * DOW` patterns (e.g. `0 8 * * 5`). Other patterns raise `ValueError` with instructions to edit the timer manually.

## Usage Tracking (`usage` command)

`usage.py` appends one JSON line per API call to `~/.local/share/auto-emailer/usage.jsonl`:

```json
{"ts": "2026-03-14T08:05:11", "type": "gemini", "model": "gemini-2.5-flash", "tokens_in": 18432, "tokens_out": 542}
{"ts": "2026-03-14T08:05:22", "type": "serper", "query": "classical music..."}
{"ts": "2026-03-14T08:05:11", "type": "jina", "url": "https://..."}
```

`log_usage()` is called from `llm.py`, `search.py` (`_serper_search` and `fetch_jina_content`). It's silent on failure — never crashes the main pipeline.

`print_report()` reads the file and prints aggregated counts filtered by month prefix.

## Config Validation

`load_config()` in `config.py`:
- Fails fast with clear error messages for missing keys (`email`, `llm.model`, `schedule.cron`, `searches`)
- Injects `GEMINI_API_KEY` and `AUTO_EMAILER_SMTP_PASSWORD` from env into config dict
- `require_secrets=False` skips env var check (used by `install` and `--dry-run`)

## Code Conventions

- **Style**: snake_case everywhere. No classes.
- **Logging**: `log = logging.getLogger(__name__)` in every module. INFO to stderr.
- **Config passing**: plain `dict` parameter through the whole call chain. No globals except the Gemini `_client` in `llm.py`.
- **Error handling**: each section in `run()` is wrapped in try/except. Errors produce a user-friendly fallback string — no raw exceptions reach the email body.
- **Imports**: stdlib → third-party → relative. Some imports deferred inside functions to avoid circular imports (`from .usage import log_usage` in `llm.py` and `search.py`).

## Debug Files (dry-run only)

| File | Contents |
|------|----------|
| `email_preview.txt` | The email body as it would have been sent |
| `search_debug.txt` | Per-section: sources fetched, date extraction stats, filter metrics, LLM output |
| `search_extract.txt` | Exact prompts sent to Gemini + LLM outputs, for quality review |

## Known Limitations

- No test suite
- No git history (no repository initialized)
- `config.json` and `keys.md` contain secrets — must not be committed
- Serper/DDG searches occasionally surface stale results; `_is_recent()` filters out results with only past-year mentions but is not foolproof
- Some event sites (Cloudflare/Incapsula/Vercel-protected) block both trafilatura and Jina — these sections get sparse or no content
