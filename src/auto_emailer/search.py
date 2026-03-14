from datetime import datetime, date
import logging
import os
import re
from urllib.parse import urlparse

import requests
import trafilatura

log = logging.getLogger(__name__)

# Curated allowlist of high-quality NYC cultural and event sources.
# Search casts a wide net, then results are filtered to prefer these domains.
# Domains consistently returning low-quality or irrelevant content.
# Results from these are filtered out of all searches before processing.
BLOCKED_DOMAINS = {
    "clubfreetime.com",     # generic "free things to do" SEO spam, no specific listings
    "allevents.in",         # low-quality aggregator with poor genre filtering
    "musicalartists.org",   # union job listings, never event listings
}

# --- Standby: keyword→URL map for rule-based anchor strategy (pre-Serper approach) ---
# Used by the commented-out anchor pass in core.py. Uncomment both to revert.
# NYEVENTS_CATEGORY_URLS: dict[str, str] = {
#     "classical": "https://new-york.events/concerts/classical/",
#     "jazz":      "https://new-york.events/concerts/jazz/",
#     "ballet":    "https://new-york.events/ballet/",
#     "opera":     "https://new-york.events/opera/",
#     "broadway":  "https://new-york.events/broadway/",
#     "concert":   "https://new-york.events/concerts/",
# }
# --- End standby ---


PREFERRED_DOMAINS = {
    # Classical music
    "carnegiehall.org",
    "nyphil.org",
    "kaufmanmusiccenter.org",
    "chambermusicsociety.org",
    "92ny.org",
    "bfrmusic.org",
    # Opera
    "metopera.org",
    # Ballet / dance
    "nycballet.com",
    "abt.org",
    "lincolncenter.org",
    # Art museums & galleries
    "metmuseum.org",
    "moma.org",
    "guggenheim.org",
    "whitney.org",
    "brooklynmuseum.org",
    "newmuseum.org",
    "nybg.org",
    "theshed.org",
    # NYC event guides (editorial, not aggregator SEO)
    "timeout.com",
    "nytimes.com",
    "newyorker.com",
    "ny1.com",
    "nydailynews.com",
    "gothamist.com",
    "theskint.com",
    "secretnyc.co",
    "playbill.com",
    "culturedmag.com",
    # Substack / curated newsletters
    "substack.com",
    # Comprehensive NYC event calendars
    "new-york.events",
}


def _domain_matches(url: str, domains: set[str]) -> bool:
    """Check if a URL's domain matches any in the given set."""
    try:
        host = urlparse(url).hostname or ""
        # Strip www. prefix for matching
        host = host.removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in domains)
    except Exception:
        return False


def _is_recent(result: dict) -> bool:
    """Check if a result appears to be from the current year.

    Scans title, snippet, extracted content, AND URL path for 4-digit year mentions.
    - No years found -> keep (likely a general/current listings page)
    - Current year found -> keep
    - Only past years found -> reject (stale content)
    """
    current_year = datetime.now().year
    href = result.get("href", "")
    text = f"{result.get('title', '')} {result.get('body', '')} {result.get('content', '')} {href}"
    years = {int(m) for m in re.findall(r'\b(20\d{2})\b', text)}
    if not years:
        return True
    if current_year in years:
        return True
    return False


_MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_DAY_NAMES = r'(?:monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thurs|friday|fri|saturday|sat|sunday|sun)'

# Matches: "Friday, February 13", "Sun Feb 22", "Tue Feb 17", "Saturday, Feb 21, 2026"
_DATE_WITH_DAY = re.compile(
    r'\b' + _DAY_NAMES + r'\s*,?\s+(' + '|'.join(_MONTH_MAP.keys()) + r')\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b',
    re.IGNORECASE,
)

# Matches: "February 13", "Feb 13", "February 13th", "Feb 2nd", "Feb 13, 2026"
_DATE_NAMED = re.compile(
    r'\b(' + '|'.join(_MONTH_MAP.keys()) + r')\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b',
    re.IGNORECASE,
)
# Matches: "2/13/2026", "2/13", "02/13/2026"
_DATE_NUMERIC = re.compile(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b')

# Matches day-number lists after a month: "FEB 11, 12, 13, 14 mat & eve, 15"
# Captures month name then a sequence of day numbers separated by commas and optional non-digit text
_MONTH_PREFIX = '|'.join(_MONTH_MAP.keys())
_DATE_LIST = re.compile(
    r'\b(' + _MONTH_PREFIX + r')\s+((?:\d{1,2})(?:\s*(?:mat|eve|&)\s*)*(?:\s*,\s*(?:\d{1,2})(?:\s*(?:mat|eve|&)\s*)*)+)',
    re.IGNORECASE,
)


def extract_dates(text: str, year: int | None = None) -> list[date]:
    """Extract all date mentions from text. Returns deduplicated list of date objects."""
    if year is None:
        year = datetime.now().year
    found = set()

    # First, extract day-number lists like "FEB 11, 12, 13, 14 mat & eve, 15"
    for m in _DATE_LIST.finditer(text):
        month_str = m.group(1)
        month = _MONTH_MAP.get(month_str.lower())
        if not month:
            continue
        day_list_str = m.group(2)
        for day_str in re.findall(r'\d{1,2}', day_list_str):
            day = int(day_str)
            try:
                found.add(date(year, month, day))
            except ValueError:
                pass

    # Extract dates with day-of-week like "Friday, Feb 20" or "Sun Feb 22"
    for m in _DATE_WITH_DAY.finditer(text):
        month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
        month = _MONTH_MAP.get(month_str.lower())
        day = int(day_str)
        y = int(year_str) if year_str else year
        try:
            found.add(date(y, month, day))
        except ValueError:
            pass

    for m in _DATE_NAMED.finditer(text):
        month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
        month = _MONTH_MAP.get(month_str.lower())
        day = int(day_str)
        y = int(year_str) if year_str else year
        try:
            found.add(date(y, month, day))
        except ValueError:
            pass

    for m in _DATE_NUMERIC.finditer(text):
        month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
        month, day = int(month_str), int(day_str)
        y = int(year_str) if year_str else year
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                found.add(date(y, month, day))
            except ValueError:
                pass

    return sorted(found)




def _smart_truncate(text: str, max_chars: int) -> str:
    """Truncate text at paragraph or sentence boundaries."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_break = truncated.rfind("\n\n")
    if last_break > max_chars // 2:
        return truncated[:last_break].rstrip()
    for sep in [". ", ".\n", "! ", "? "]:
        last_sentence = truncated.rfind(sep)
        if last_sentence > max_chars // 2:
            return truncated[:last_sentence + 1].rstrip()
    return truncated.rstrip()


_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def fetch_jina_content(url: str, timeout: int = 15, max_chars: int = 3000) -> str | None:
    """Fetch a URL via Jina Reader API (renders JS server-side). Returns extracted text or None."""
    jina_url = f"https://r.jina.ai/{url}"
    try:
        resp = requests.get(jina_url, headers={"Accept": "text/plain"}, timeout=timeout)
        if resp.status_code != 200:
            log.debug(f"Jina returned {resp.status_code} for {url}")
            return None
        text = resp.text.strip()
        if not text:
            return None
        from .usage import log_usage
        log_usage("jina", url=url[:120])
        return _smart_truncate(text, max_chars)
    except Exception as e:
        log.debug(f"Jina fetch failed for {url}: {e}")
        return None


def fetch_page_content(url: str, timeout: int = 10, max_chars: int = 3000) -> str | None:
    """Fetch a URL and extract main text content.

    Tries trafilatura first (fast, no external API). Falls back to Jina Reader
    (renders JS server-side) when trafilatura returns no text or significantly
    less than max_chars (indicating the page requires JS rendering for full content).
    """
    trafilatura_text = None
    # Try trafilatura first
    try:
        config = trafilatura.settings.use_config()
        config.set("DEFAULT", "USER_AGENTS", _USER_AGENT)
        downloaded = trafilatura.fetch_url(url, config=config)
        if downloaded:
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=True,
                favor_recall=True,
            )
            if text:
                trafilatura_text = _smart_truncate(text, max_chars)
    except Exception as e:
        log.debug(f"trafilatura failed for {url}: {e}, trying Jina")

    # If trafilatura returned content filling ≥75% of the limit, it's likely complete.
    # Otherwise try Jina: JS-rendered event calendars often have more content than
    # static extraction can see.
    if trafilatura_text and len(trafilatura_text) >= max_chars * 0.75:
        return trafilatura_text

    jina_text = fetch_jina_content(url, max_chars=max_chars)
    if jina_text:
        # Keep whichever source returned more content
        if trafilatura_text and len(trafilatura_text) > len(jina_text):
            return trafilatura_text
        log.info(f"  Jina fallback succeeded for {url} ({len(jina_text)} chars)")
        return jina_text

    return trafilatura_text



def web_search(
    query: str,
    config: dict | None = None,
    num_results: int = 5,
    timelimit: str = "m",
    region: str = "us-en",
) -> list[dict]:
    """Search the web. Priority: Serper (Google) → Brave → DDG.

    Returns list of {"title": str, "href": str, "content": str} dicts.
    content is the result snippet; callers can use fetch_page_content() for full text.
    timelimit: 'd'=day, 'w'=week, 'm'=month, 'y'=year (only used by Brave/DDG fallbacks).
    """
    cfg = config or {}
    serper_key = cfg.get("search", {}).get("serper_api_key") or os.environ.get("SERPER_API_KEY", "")
    brave_key = cfg.get("search", {}).get("brave_api_key") or os.environ.get("BRAVE_SEARCH_API_KEY", "")

    if serper_key:
        results = _serper_search(query, serper_key, num_results=num_results)
        if results:
            return [r for r in results if not _domain_matches(r["href"], BLOCKED_DOMAINS)]

    if brave_key:
        results = _brave_search(query, brave_key, num_results=num_results, timelimit=timelimit)
    else:
        results = _ddg_search(query, num_results=num_results, timelimit=timelimit, region=region)

    return [r for r in results if not _domain_matches(r["href"], BLOCKED_DOMAINS)]


def _serper_search(
    query: str,
    api_key: str,
    num_results: int = 10,
) -> list[dict]:
    """Search via Serper.dev (Google Search API). 2,500 free queries/month."""
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": min(num_results, 10), "gl": "us", "hl": "en"},
            timeout=15,
        )
        resp.raise_for_status()
        results = [
            {
                "title": r.get("title", ""),
                "href": r.get("link", ""),
                "content": r.get("snippet", ""),
            }
            for r in resp.json().get("organic", [])
        ]
        log.info(f"  Serper search '{query[:60]}': {len(results)} results")
        from .usage import log_usage
        log_usage("serper", query=query[:80])
        return results
    except Exception as e:
        log.warning(f"Serper search failed for '{query}': {e} — falling back to Brave/DDG")
        return []


def _brave_search(
    query: str,
    api_key: str,
    num_results: int = 10,
    timelimit: str = "m",
) -> list[dict]:
    """Search via Brave Search API. Requires a free API key (1,000 queries/month)."""
    # Brave freshness codes: pd=day, pw=week, pm=month, py=year
    freshness_map = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}
    params: dict = {
        "q": query,
        "count": min(num_results, 20),
        "country": "us",
        "search_lang": "en",
        "result_filter": "web",
    }
    if timelimit and timelimit in freshness_map:
        params["freshness"] = freshness_map[timelimit]

    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("web", {}).get("results", [])
        results = [
            {
                "title": r.get("title", ""),
                "href": r.get("url", ""),
                "content": r.get("description", ""),
            }
            for r in items
        ]
        log.info(f"  Brave search '{query[:60]}': {len(results)} results")
        return results
    except Exception as e:
        log.warning(f"Brave search failed for '{query}': {e} — falling back to DDG")
        return _ddg_search(query, num_results=num_results, timelimit=timelimit)


def _ddg_search(
    query: str,
    num_results: int = 5,
    timelimit: str = "m",
    region: str = "us-en",
) -> list[dict]:
    """Search via DuckDuckGo (fallback, no API key required)."""
    try:
        from ddgs import DDGS
        kwargs: dict = {"max_results": num_results}
        if timelimit:
            kwargs["timelimit"] = timelimit
        if region:
            kwargs["region"] = region
        raw = DDGS().text(query, **kwargs)
        results = [
            {
                "title": r.get("title", ""),
                "href": r.get("href", ""),
                "content": r.get("body", ""),
            }
            for r in (raw or [])
        ]
        log.info(f"  DDG search '{query[:60]}': {len(results)} results")
        return results
    except Exception as e:
        log.warning(f"DDG search failed for '{query}': {e}")
        return []
