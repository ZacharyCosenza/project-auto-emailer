from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
import logging
import re
import time

from .llm import generate_response
from .search import (
    extract_dates, fetch_page_content,
    _domain_matches, _is_recent, PREFERRED_DOMAINS, web_search,
)
from .email import send_email

log = logging.getLogger(__name__)


def get_weekend_dates() -> tuple[str, str, str, str]:
    """Get the upcoming weekend dates (Friday through Sunday)."""
    friday, sunday = get_weekend_datetime_range()
    fri_full = friday.strftime("%B %d, %Y")
    sun_full = sunday.strftime("%B %d, %Y")
    fri_short = friday.strftime("%b %d")
    sun_short = sunday.strftime("%b %d")
    return fri_full, sun_full, fri_short, sun_short


def get_weekend_datetime_range() -> tuple[datetime, datetime]:
    """Get Friday and Sunday as datetime objects for the upcoming weekend."""
    today = datetime.now()
    days_until_friday = (4 - today.weekday()) % 7
    friday = today + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)
    return friday, sunday


def search_events(
    topic: str,
    config: dict,
    sources: list[str] | None = None,
    debug_sections: list | None = None,
    max_events: int | None = None,
    extra_instructions: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """Search the web for the topic and summarize weekend events.

    sources: optional list of URLs to always fetch alongside search results.
             Use in config.json to guarantee comprehensive listing pages are included,
             e.g. new-york.events category pages that Serper may not surface consistently.
    max_events: if set, instructs the LLM to cap output at this many events.
    extra_instructions: additional per-section prompt rules appended after the base rules.
    """
    log.info(f"Processing: {topic}")
    fri_full, sun_full, fri_short, sun_short = get_weekend_dates()
    friday, sunday = get_weekend_datetime_range()
    target_dates = {friday.date() + timedelta(days=i) for i in range(3)}  # Fri, Sat, Sun

    num_results = (config.get("search") or {}).get("max_results", 5)
    scrape_config = (config or {}).get("scrape", {})
    max_chars = scrape_config.get("max_chars_per_page", 20000)

    # Strip "this weekend" and append the concrete Friday date so search engines
    # return current event calendar pages rather than general season guides.
    search_topic = re.sub(r'\bthis\s+weekend\b', '', topic, flags=re.IGNORECASE).strip(" ,")
    query = f"{search_topic} {friday.strftime('%B %d %Y')}"

    seen_hrefs: set[str] = set()

    # Explicit sources from config — fetched sequentially with retry, separate from the
    # parallel Serper pool. These are guaranteed seeds (e.g. new-york.events category pages)
    # and need reliable fetching; bundling them with parallel Serper fetches risks Jina
    # rate-limit failures that leave them with empty content and exclude them from the prompt.
    fetched_explicit: list[dict] = []
    for url in (sources or []):
        if url in seen_hrefs:
            continue
        seen_hrefs.add(url)
        result: dict = {"title": url, "href": url, "body": "", "content": ""}
        for attempt in range(3):
            full = fetch_page_content(url, max_chars=max_chars)
            if full and len(full) > 500:
                result["content"] = full
                break
            if attempt < 2:
                log.debug(f"Explicit source fetch attempt {attempt+1} returned thin content for {url}, retrying...")
                time.sleep(2 ** attempt)  # 1s, 2s backoff
        if result["content"]:
            log.info(f"  Explicit source fetched: {url} ({len(result['content'])} chars)")
        else:
            log.warning(f"  Explicit source returned no content after 3 attempts: {url}")
        fetched_explicit.append(result)

    # --- Standby: rule-based anchor pass (pre-Serper approach) ---
    # Uncomment to revert to the keyword→URL strategy if Serper proves unreliable.
    # from .search import NYEVENTS_CATEGORY_URLS
    # anchor_sub_topics = re.split(r'\s+and\s+', search_topic, flags=re.IGNORECASE)
    # for sub in anchor_sub_topics:
    #     sub_lower = sub.strip().lower()
    #     matched_url = next(
    #         (url for kw, url in NYEVENTS_CATEGORY_URLS.items() if kw in sub_lower), None
    #     )
    #     if matched_url and matched_url not in seen_hrefs:
    #         seen_hrefs.add(matched_url)
    #         pending.append({"title": f"new-york.events: {sub.strip()}", "href": matched_url, "content": ""})
    #     else:
    #         nyevents_query = f"site:new-york.events {sub.strip()}"
    #         for r in web_search(nyevents_query, config, num_results=3):
    #             if r["href"] not in seen_hrefs:
    #                 seen_hrefs.add(r["href"])
    #                 pending.append(r)
    # --- End standby ---

    pending: list[dict] = []
    for r in web_search(query, config, num_results=num_results):
        if r["href"] not in seen_hrefs and _is_recent(r):
            seen_hrefs.add(r["href"])
            pending.append(r)

    # Fetch Serper results in parallel (up to 5 concurrent requests).
    # Prepend the search snippet to the fetched page — snippets from Serper often contain
    # high-density date/event info (e.g. "FEB 27, 28, MAR 1") that JS-rendered pages
    # hide from our scraper. Combining both ensures dates are never lost.
    def _fetch_one(r: dict) -> dict:
        snippet = r.get("content", "")
        full = fetch_page_content(r["href"], max_chars=max_chars)
        content = f"{snippet}\n\n{full}" if full else snippet
        return {"title": r["title"], "href": r["href"], "body": "", "content": content}

    serper_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(pending), 5)) as pool:
        futures = {pool.submit(_fetch_one, r): r for r in pending}
        for future in as_completed(futures):
            try:
                serper_results.append(future.result())
            except Exception as e:
                log.debug(f"Fetch failed for {futures[future]['href']}: {e}")

    # Explicit sources first (guaranteed seeds), then Serper results
    all_results: list[dict] = fetched_explicit + serper_results

    if not all_results:
        return "No results found. Try searching for events directly.", []

    combined = "\n\n".join(
        f"Source: {r['href']}\n{r['content']}" for r in all_results if r["content"]
    )

    # Two-pass filter: identify results with weekend dates, build prompt from ONLY those
    weekend_results = []
    other_results = []
    for r in all_results:
        content = r.get("content", "")
        if not content:
            continue
        result_dates = set(extract_dates(content))
        if result_dates & target_dates:
            weekend_results.append(r)
        else:
            other_results.append(r)

    all_dates = extract_dates(combined)
    weekend_dates = [d for d in all_dates if d in target_dates]

    today_str = datetime.now().strftime("%A, %B %d, %Y")
    matched = ""
    unmatched = combined

    if weekend_results and weekend_dates:
        # Case 1: Specific dates matching this weekend were found
        date_list = ", ".join(d.strftime("%b %d") for d in sorted(set(weekend_dates)))

        # Rank weekend results: preferred editorial/venue domains first, then by content length
        def _rank_key(r: dict) -> tuple:
            is_pref = _domain_matches(r["href"], PREFERRED_DOMAINS)
            return (0 if is_pref else 1, -len(r.get("content", "")))
        top_results = sorted(weekend_results, key=_rank_key)[:5]
        weekend_content_parts = []
        for r in top_results:
            weekend_content_parts.append(f"Source: {r['href']}\n{r.get('content', '')[:18000]}")
        weekend_content = "\n\n".join(weekend_content_parts)

        rules = [
            "Only include events that MATCH the search topic. Exclude anything clearly outside the topic scope.",
            f"Only include events with confirmed dates between {fri_short} and {sun_short}.",
            "List NYC venues before NJ/CT venues. Note out-of-city locations.",
            "If the same event occurs at multiple times on the same day, list it once with all times rather than a separate bullet per time.",
            "If a source has no relevant weekend events, do not list it.",
        ]
        if max_events:
            rules.insert(0, f"List at most {max_events} events total, prioritising the most notable.")
        if extra_instructions:
            rules.extend(extra_instructions)
        rules_str = "\n".join(f"- {r}" for r in rules)

        prompt = f'''Today is {today_str}. The following content from {len(weekend_results)} sources contains events with specific dates matching this weekend ({fri_short}-{sun_short}).

Weekend dates found: {date_list}

SEARCH TOPIC: {topic}

WEEKEND EVENT CONTENT:
{weekend_content}

Create a bulleted list of confirmed performances that are RELEVANT TO THE SEARCH TOPIC above, for this weekend ({fri_short}-{sun_short}). For each event, provide:

• **Event Title** - Day, Time - Venue Name [→](URL)

For the URL: use any event-specific link found in the content (e.g., an "Event Info" link for that specific event). If no specific link is available, use the Source URL.
Rules:
{rules_str}'''

        matched = weekend_content
        unmatched = "\n\n".join(f"Source: {r['href']}\n{r.get('content', '')}" for r in other_results)

    elif all_dates and not weekend_dates:
        # Case 2: Specific dates found but NONE match this weekend
        upcoming = sorted(d for d in all_dates if d >= friday.date())[:5]
        upcoming_str = ", ".join(d.strftime("%b %d") for d in upcoming) if upcoming else "none in the near future"
        prompt = f'''Today is {today_str}. The following sources list specific upcoming performance dates, but NONE fall on this weekend ({fri_short}-{sun_short}).

Next upcoming dates found: {upcoming_str}

{combined[:10000]}

Based on the specific performance dates listed, are there any events confirmed for this weekend ({fri_short}-{sun_short})? If not, state clearly that no performances are scheduled for this weekend, and mention when the next upcoming performances are.'''

    else:
        # Case 3: No specific dates found at all
        prompt = f'''Today is {today_str}. The following content is from event listing sources. NOTE: No specific performance dates were found — the sources only provide general information.

{combined[:10000]}

What performances might be happening this weekend ({fri_short}-{sun_short})? Write 2-3 sentences. Be clear that specific dates could not be confirmed.'''

    # Capture debug info before the LLM call so it's preserved even if the call fails.
    debug_entry: dict | None = None
    if debug_sections is not None:
        debug_entry = {
            "section": topic, "results": all_results,
            "context_chars": len(combined), "prompt_chars": len(prompt),
            "prompt": prompt, "summary": "",
            "dates_found": [d.isoformat() for d in weekend_dates],
            "all_dates_count": len(all_dates),
            "subpages_fetched": 0,
            "weekend_results_count": len(weekend_results),
            "total_results_count": len(all_results),
            "matched_chars": len(matched), "unmatched_chars": len(unmatched),
        }
        debug_sections.append(debug_entry)

    summary = generate_response(prompt, config)

    if debug_entry is not None:
        debug_entry["summary"] = summary

    return summary, all_results


def search_summary(topic: str, config: dict, debug_sections: list | None = None) -> tuple[str, list[dict]]:
    """Search and summarize without event-specific date filtering.
    Suitable for weather, transit, news, or any non-event query.
    """
    log.info(f"Processing (summary): {topic}")
    friday, _ = get_weekend_datetime_range()
    fri_short = friday.strftime("%b %d")
    sun_short = (friday + timedelta(days=2)).strftime("%b %d")

    num_results = (config.get("search") or {}).get("max_results", 5)
    scrape_config = (config or {}).get("scrape", {})
    max_chars = scrape_config.get("max_chars_per_page", 20000)

    search_topic = re.sub(r'\bthis\s+weekend\b', '', topic, flags=re.IGNORECASE).strip(" ,")
    fri_date = friday.strftime('%B %d %Y')
    query = f"{search_topic} {fri_date}"

    raw_results = web_search(query, config, num_results=num_results, region="us-en")
    seen: set[str] = set()
    pending = []
    for r in raw_results:
        if r["href"] not in seen and _is_recent(r):
            seen.add(r["href"])
            pending.append(r)

    def _fetch_one(r: dict) -> dict:
        snippet = r.get("content", "")
        full = fetch_page_content(r["href"], max_chars=max_chars)
        content = f"{snippet}\n\n{full}" if full else snippet
        return {"title": r["title"], "href": r["href"], "body": "", "content": content}

    all_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(pending), 5)) as pool:
        futures = {pool.submit(_fetch_one, r): r for r in pending}
        for future in as_completed(futures):
            try:
                all_results.append(future.result())
            except Exception as e:
                log.debug(f"Fetch failed for {futures[future]['href']}: {e}")

    if not all_results:
        return f"No information found for: {topic}", []

    # Use top 3 by content length
    top = sorted(all_results, key=lambda r: -len(r.get("content", "")))[:3]
    context = "\n\n".join(
        f"Source: {r['href']}\n{r.get('content', '')[:5000]}"
        for r in top if r.get("content")
    )

    today_str = datetime.now().strftime("%A, %B %d, %Y")
    prompt = f'''Today is {today_str}. Summarize the following information about "{topic}" for this weekend ({fri_short}–{sun_short}) in New York City. Be specific and concise. If details apply to a specific day, note it.

{context}'''

    debug_entry_s: dict | None = None
    if debug_sections is not None:
        debug_entry_s = {
            "section": topic, "type": "summary", "results": all_results,
            "context_chars": len(context), "prompt_chars": len(prompt),
            "prompt": prompt, "summary": "",
        }
        debug_sections.append(debug_entry_s)

    summary = generate_response(prompt, config)

    if debug_entry_s is not None:
        debug_entry_s["summary"] = summary

    return summary, all_results


def format_email(sections: dict[str, tuple[str, list[dict]]], labels: dict[str, str] | None = None) -> str:
    fri_full, sun_full, _, _ = get_weekend_dates()
    body = f"NYC Weekend Guide\n{fri_full} - {sun_full}\n{'='*50}\n\n"

    for topic, (summary, results) in sections.items():
        if labels and topic in labels:
            label = labels[topic]
        else:
            label = topic.replace("NYC", "").replace("this weekend", "").replace("New York City", "").strip(" ,").title()
        body += f"## {label}\n\n{summary}\n\n"
        # Only show fallback links if no specific events were listed in summary
        if results and (not summary or len(summary) < 200):
            seen = set()
            body += "\nSources:\n"
            for r in results:
                if r["href"] not in seen:
                    seen.add(r["href"])
                    body += f"• {r['title']}\n  {r['href']}\n"
        body += "\n"

    body += "---\n⚠️ This is an AI-generated summary. Always verify at the source links.\n"
    return body


def _write_debug(sections: list, path: str = "search_debug.txt"):
    """Write debug info and full LLM extracts to files for quality evaluation."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')

    # --- search_debug.txt: metadata + result inventory ---
    lines = [f"SEARCH + LLM DEBUG - {ts}", "=" * 70]
    for section in sections:
        lines.append(f"\n### {section['section']} [{section.get('type', 'events')}]")
        lines.append("-" * 70)

        if "dates_found" in section:
            lines.append(f"  WEEKEND DATES: {section['dates_found']}")
            lines.append(f"  ALL DATES EXTRACTED: {section.get('all_dates_count', 0)}")
            lines.append(f"  WEEKEND RESULTS: {section.get('weekend_results_count', 0)}/{section.get('total_results_count', 0)} results")
            lines.append(f"  MATCHED CONTENT: {section.get('matched_chars', 0)} chars")
            lines.append(f"  UNMATCHED CONTENT: {section.get('unmatched_chars', 0)} chars")

        for i, r in enumerate(section["results"]):
            href = r.get("href", "")
            is_pref = _domain_matches(href, PREFERRED_DOMAINS)
            content = r.get("content", "")
            lines.append(f"  RESULT {i+1} {'[PREFERRED]' if is_pref else '[other]'}:")
            lines.append(f"    title:   {r.get('title', 'N/A')}")
            lines.append(f"    href:    {href}")
            lines.append(f"    content: {len(content)} chars")
            lines.append(f"    preview: {content[:200]}")
        lines.append(f"\n  RAW CONTENT: {section['context_chars']} chars total")
        if "prompt_chars" in section:
            lines.append(f"  PROMPT SENT TO LLM: {section['prompt_chars']} chars")
        lines.append(f"  LLM OUTPUT:\n{section['summary']}")

    with open(path, "w") as f:
        f.write("\n".join(lines))
    log.info(f"Debug written to {path}")

    # --- search_extract.txt: the exact text sent to the LLM, for human review ---
    extract_lines = [f"LLM EXTRACTS - {ts}", "=" * 70]
    for section in sections:
        extract_lines.append(f"\n{'=' * 70}")
        extract_lines.append(f"TOPIC: {section['section']}  [{section.get('type', 'events')}]")
        extract_lines.append(f"PROMPT ({section.get('prompt_chars', '?')} chars):")
        extract_lines.append("-" * 70)
        extract_lines.append(section.get("prompt", ""))
        extract_lines.append(f"\nLLM OUTPUT:")
        extract_lines.append("-" * 70)
        extract_lines.append(section.get("summary", ""))

    with open("search_extract.txt", "w") as f:
        f.write("\n".join(extract_lines))
    log.info("Extract written to search_extract.txt")


def run(config: dict, dry_run: bool = False):
    log.info("Starting run")
    debug_sections = []
    call_delay = config.get("llm", {}).get("call_delay", 5)
    global_instructions = config.get("instructions", [])

    sections = {}
    labels: dict[str, str] = {}
    for i, item in enumerate(config.get("searches", config.get("events", []))):
        # Support both plain strings (default: events) and {"query": ..., "type": ...} dicts
        if isinstance(item, str):
            query, search_type, sources, max_events, extra_instructions, label = item, "events", [], None, None, None
        else:
            query = item.get("query", "")
            search_type = item.get("type", "events")
            sources = item.get("sources", [])
            max_events = item.get("max_events")
            extra_instructions = item.get("instructions")
            label = item.get("label")

        if label:
            labels[query] = label

        # Merge global instructions with per-section instructions
        combined_instructions = (list(global_instructions) + list(extra_instructions or [])) or None

        if not query:
            continue

        if i > 0 and call_delay > 0:
            log.info(f"Pacing: waiting {call_delay}s before next API call")
            time.sleep(call_delay)

        try:
            if search_type == "summary":
                sections[query] = search_summary(query, config, debug_sections=debug_sections)
            else:
                sections[query] = search_events(
                    query, config, sources=sources, debug_sections=debug_sections,
                    max_events=max_events, extra_instructions=combined_instructions,
                )
        except Exception as e:
            log.error(f"Error processing '{query}': {e}")
            sections[query] = ("No summary available. Check the source links.", [])

    body = format_email(sections, labels=labels)

    if dry_run:
        print(body)
        _write_debug(debug_sections)
        with open("email_preview.txt", "w") as f:
            f.write(body)
        log.info("Email preview written to email_preview.txt")
        return

    subject = f"{config['email']['subject_prefix']} - {get_weekend_dates()[0]}"
    send_email(config["email"], subject, body)
    log.info("Email sent")
