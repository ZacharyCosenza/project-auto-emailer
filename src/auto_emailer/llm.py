import logging
import time

from google import genai
from google.genai import types

log = logging.getLogger(__name__)

_client = None


def _get_client(api_key: str) -> genai.Client:
    global _client
    if _client is None:
        if not api_key:
            raise ValueError(
                "Gemini API key is empty. Set the GEMINI_API_KEY environment variable."
            )
        _client = genai.Client(api_key=api_key)
        log.info("Gemini client initialized")
    return _client


def generate_response(prompt: str, config: dict, max_retries: int = 3) -> str:
    llm_config = config["llm"]
    client = _get_client(llm_config["api_key"])
    model = llm_config.get("model", "gemini-2.5-flash")

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=llm_config.get("temperature", 0),
                    max_output_tokens=llm_config.get("max_output_tokens", 2048),
                ),
            )
            from .usage import log_usage
            usage = getattr(response, "usage_metadata", None)
            log_usage("gemini", model=model,
                      tokens_in=getattr(usage, "prompt_token_count", 0),
                      tokens_out=getattr(usage, "candidates_token_count", 0))
            text = response.text or ""
            return _truncate_repetition(text)
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = 45 * (attempt + 1)
                log.warning(f"Rate limited (attempt {attempt + 1}/{max_retries}), waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Rate limited after {max_retries} retries")


def _truncate_repetition(text: str, min_len: int = 40) -> str:
    """Safety net: detect and remove degenerate repetition loops."""
    paragraphs = text.split("\n\n")
    if len(paragraphs) < 3:
        return text
    seen = set()
    kept = []
    for p in paragraphs:
        normalized = p.strip().lower()[:min_len]
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        kept.append(p)
    return "\n\n".join(kept)
