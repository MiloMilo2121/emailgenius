from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

from duckduckgo_search import DDGS
try:
    from duckduckgo_search.exceptions import RatelimitException as _DDGRatelimit
except ImportError:
    _DDGRatelimit = None
from duckduckgo_search.exceptions import DuckDuckGoSearchException

from tavily import TavilyClient
from .types import SearchHit

DEFAULT_TIMEOUT_S = 15
logger = logging.getLogger(__name__)

BLOCKED_OFFICIAL_SITE_DOMAINS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "wikipedia.org",
    "it.wikipedia.org",
    "paginegialle.it",
    "indeed.com",
    "glassdoor.com",
}

COMPANY_TOKEN_STOPWORDS = {
    "spa",
    "srl",
    "srls",
    "srlu",
    "sapa",
    "societa",
    "group",
    "gruppo",
    "holding",
    "company",
    "co",
    "italia",
    "italy",
}

EVENT_NEWS_KEYWORDS = (
    "invest",
    "investment",
    "capex",
    "impiant",
    "stabiliment",
    "espans",
    "acquis",
    "partnership",
    "accord",
    "joint venture",
    "funding",
    "fiera",
    "fiere",
    "expo",
    "salone",
    "evento",
    "eventi",
    "conference",
    "webinar",
    "lancio",
    "launch",
    "avvia",
    "produttiv",
    "nuovo stabilimento",
    "nuovo impianto",
    "commessa",
    "ordine",
    "contract",
    "premio",
    "award",
    "risultati",
    "fatturato",
    "bilancio",
)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]{3,}", text.lower())


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_homepage_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}/"


def _get_tavily_client() -> TavilyClient:
    api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is required to perform web search")
    return TavilyClient(api_key=api_key)


def _map_tavily_hits(raw_results: object, *, max_results: int) -> list[SearchHit]:
    if not isinstance(raw_results, list):
        return []
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("content") or "").strip()
        if not title or not url or not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= max_results:
            break
    return hits


def _map_ddg_hits(raw_results: object, *, max_results: int) -> list[SearchHit]:
    if not isinstance(raw_results, list):
        return []
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or item.get("href") or "").strip()
        snippet = str(item.get("body") or item.get("content") or item.get("snippet") or "").strip()
        if not title or not url or not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= max_results:
            break
    return hits


def search_news_web(query: str, *, max_results: int = 8, timeout_s: int = DEFAULT_TIMEOUT_S) -> list[SearchHit]:
    try:
        client = _get_tavily_client()
        payload = client.search(query=query, max_results=max_results, search_depth="basic")
        hits = _map_tavily_hits(payload.get("results"), max_results=max_results)
    except Exception:
        logger.warning("Tavily failed, falling back to DDG for news search...", exc_info=False)
        try:
            hits = _map_ddg_hits(list(DDGS().news(query, max_results=max_results)), max_results=max_results)
        except Exception as exc:
            if _DDGRatelimit and isinstance(exc, _DDGRatelimit):
                logger.warning("DDG rate limit hit for query: %s", query)
            elif "RatelimitException" in str(type(exc)) or "Ratelimit" in str(exc):
                logger.warning("DDG news fallback rate limited for query: %s", query)
            else:
                logger.warning("DDG news fallback failed for query: %s", query, exc_info=True)
            hits = []

    filtered: list[SearchHit] = []
    for hit in hits:
        host = _domain(hit.url)
        if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_OFFICIAL_SITE_DOMAINS):
            continue
        filtered.append(hit)
    return filtered


def search_web(query: str, *, max_results: int = 8, timeout_s: int = DEFAULT_TIMEOUT_S) -> list[SearchHit]:
    try:
        client = _get_tavily_client()
        payload = client.search(query=query, max_results=max_results, search_depth="basic")
        return _map_tavily_hits(payload.get("results"), max_results=max_results)
    except Exception:
        logger.warning("Tavily failed, falling back to DDG for web search...", exc_info=False)
        try:
            return _map_ddg_hits(list(DDGS().text(query, max_results=max_results)), max_results=max_results)
        except Exception as exc:
            if _DDGRatelimit and isinstance(exc, _DDGRatelimit):
                logger.warning("DDG rate limit hit for query: %s", query)
            elif "RatelimitException" in str(type(exc)) or "Ratelimit" in str(exc):
                logger.warning("DDG web fallback rate limited for query: %s", query)
            else:
                logger.warning("DDG web fallback failed for query: %s", query, exc_info=True)
            return []


def build_site_query(company_name: str, city: str | None = None) -> str:
    if city:
        return f"{company_name} {city} sito ufficiale"
    return f"{company_name} sito ufficiale"


def build_news_query(company_name: str, city: str | None = None) -> str:
    if city:
        return f"{company_name} {city} news"
    return f"{company_name} news"


def build_company_news_queries(
    company_name: str,
    city: str | None = None,
    *,
    official_domain: str | None = None,
) -> list[str]:
    base = build_news_query(company_name, city)
    city_part = f" {city}" if city else ""

    queries = [
        f"{company_name}{city_part} investimenti fiere partnership acquisizioni",
        base,
    ]
    if official_domain:
        queries.extend(
            [
                f"site:{official_domain} {company_name} news",
                f"site:{official_domain} {company_name} comunicato stampa",
                f"site:{official_domain} {company_name} fiere eventi",
            ]
        )

    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        value = " ".join(query.split()).strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def select_official_site(company_name: str, city: str | None, candidates: list[SearchHit]) -> SearchHit | None:
    if not candidates:
        return None

    company_tokens = _tokenize(company_name)
    city_tokens = _tokenize(city or "")

    def score(hit: SearchHit) -> int:
        rank = 0
        host = _domain(hit.url)
        text = f"{hit.title} {hit.snippet}".lower()

        if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_OFFICIAL_SITE_DOMAINS):
            rank -= 40

        for token in company_tokens:
            if token in host:
                rank += 12
            if token in text:
                rank += 5

        for token in city_tokens:
            if token in host:
                rank += 8
            if token in text:
                rank += 4

        if "ufficiale" in text:
            rank += 8
        if "azienda" in text:
            rank += 4

        if "/news" in hit.url or "/blog" in hit.url:
            rank -= 6

        return rank

    ranked = sorted(candidates, key=score, reverse=True)
    selected = ranked[0]
    return SearchHit(
        title=selected.title,
        url=normalize_homepage_url(selected.url),
        snippet=selected.snippet,
    )


def _company_tokens_for_news(company_name: str) -> list[str]:
    tokens = [item for item in re.findall(r"[a-z0-9]{2,}", company_name.lower()) if item not in COMPANY_TOKEN_STOPWORDS]
    if tokens:
        return tokens
    return re.findall(r"[a-z0-9]{2,}", company_name.lower())


def _is_ambiguous_short_company_name(company_name: str) -> bool:
    tokens = _company_tokens_for_news(company_name)
    if len(tokens) != 1:
        return False
    return len(tokens[0]) <= 3


def _match_company_in_text(*, company_name: str, text: str, host: str) -> bool:
    normalized_name = " ".join(company_name.lower().split()).strip()
    if normalized_name and normalized_name in text:
        return True

    tokens = _company_tokens_for_news(company_name)
    if not tokens:
        return False

    matched_tokens: set[str] = set()
    for token in tokens:
        token_pattern = rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])"
        if re.search(token_pattern, text) or token in host:
            matched_tokens.add(token)

    if len(tokens) == 1:
        return bool(matched_tokens)

    strong_tokens = [item for item in tokens if len(item) >= 5]
    if len(strong_tokens) >= 2:
        strong_matches = sum(1 for item in strong_tokens if item in matched_tokens)
        return strong_matches >= 2

    if len(strong_tokens) == 1:
        strong = strong_tokens[0]
        if strong not in matched_tokens:
            return False
        secondary_tokens = [item for item in tokens if item != strong and len(item) >= 3]
        if any(item in matched_tokens for item in secondary_tokens):
            return True
        # Relax only for clearly official-looking URLs when the main brand token matches.
        return strong in host and any(token in text for token in ("news", "press", "blog", "media"))

    return len(matched_tokens) >= 2


def is_event_news_hit(hit: SearchHit) -> bool:
    text = " ".join(
        (
            str(hit.title or "").lower(),
            str(hit.snippet or "").lower(),
            str(hit.url or "").lower(),
        )
    )
    return any(keyword in text for keyword in EVENT_NEWS_KEYWORDS)


def _news_relevance_score(*, hit: SearchHit, company_name: str, selected_site: SearchHit | None) -> int:
    host = _domain(hit.url)
    text = " ".join(
        (
            str(hit.title or "").lower(),
            str(hit.snippet or "").lower(),
            str(hit.url or "").lower(),
        )
    )
    selected_domain = _domain(selected_site.url) if selected_site else ""

    score = 0
    if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_OFFICIAL_SITE_DOMAINS):
        score -= 100

    if selected_domain and (host == selected_domain or host.endswith(f".{selected_domain}")):
        score += 30
    elif selected_domain and _is_ambiguous_short_company_name(company_name):
        # Avoid drifting to unrelated large brands for short/ambiguous names (e.g. "3M").
        score -= 40

    if _match_company_in_text(company_name=company_name, text=text, host=host):
        score += 24
    else:
        score -= 30

    event_hits = sum(1 for keyword in EVENT_NEWS_KEYWORDS if keyword in text)
    score += min(32, event_hits * 4)

    if any(token in hit.url.lower() for token in ("/news", "/press", "/media", "/blog")):
        score += 4
    if re.search(r"\b20[0-9]{2}\b", text):
        score += 2

    return score


def _filter_news_results(
    *,
    news_results: list[SearchHit],
    company_name: str,
    selected_site: SearchHit | None,
    max_results: int,
) -> list[SearchHit]:
    if not news_results:
        return []

    selected_domain = _domain(selected_site.url) if selected_site else ""
    is_short_name = _is_ambiguous_short_company_name(company_name)
    ranked: list[tuple[int, int, SearchHit]] = []
    seen_urls: set[str] = set()

    for index, hit in enumerate(news_results):
        if not hit.url:
            continue
        url = hit.url.strip()
        if url in seen_urls:
            continue
        seen_urls.add(url)

        host = _domain(hit.url)
        if is_short_name and selected_domain and not (host == selected_domain or host.endswith(f".{selected_domain}")):
            continue
        if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_OFFICIAL_SITE_DOMAINS):
            continue

        score = _news_relevance_score(hit=hit, company_name=company_name, selected_site=selected_site)
        if score <= 0:
            continue
        ranked.append((score, index, hit))

    if not ranked:
        fallback: list[SearchHit] = []
        for hit in news_results:
            host = _domain(hit.url)
            if is_short_name and selected_domain and not (host == selected_domain or host.endswith(f".{selected_domain}")):
                continue
            if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_OFFICIAL_SITE_DOMAINS):
                continue
            text = " ".join(
                (
                    str(hit.title or "").lower(),
                    str(hit.snippet or "").lower(),
                    str(hit.url or "").lower(),
                )
            )
            if not _match_company_in_text(company_name=company_name, text=text, host=host):
                continue
            fallback.append(hit)
            if len(fallback) >= max_results:
                break
        return fallback

    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked[:max_results]]


def collect_company_news(
    *,
    company_name: str,
    city: str | None,
    selected_site: SearchHit | None,
    news_max_results: int = 8,
) -> tuple[str, list[SearchHit]]:
    selected_domain = _domain(selected_site.url) if selected_site else ""
    queries = build_company_news_queries(
        company_name,
        city,
        official_domain=selected_domain or None,
    )
    news_query = queries[0] if queries else build_news_query(company_name, city)

    collected_hits: list[SearchHit] = []
    seen_urls: set[str] = set()
    per_query_results = max(news_max_results * 2, news_max_results)
    for query in queries:
        hits = search_news_web(query, max_results=per_query_results)
        for hit in hits:
            url = hit.url.strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            collected_hits.append(hit)
        if len(collected_hits) >= max(news_max_results * 4, news_max_results):
            break

    filtered = _filter_news_results(
        news_results=collected_hits,
        company_name=company_name,
        selected_site=selected_site,
        max_results=news_max_results,
    )
    if filtered:
        return news_query, filtered

    fallback_query = build_news_query(company_name, city)
    fallback_raw = search_news_web(fallback_query, max_results=news_max_results)
    fallback = _filter_news_results(
        news_results=fallback_raw,
        company_name=company_name,
        selected_site=selected_site,
        max_results=news_max_results,
    )
    return fallback_query, fallback[:news_max_results]


def discover_company_and_news(
    *,
    company_name: str,
    city: str | None,
    site_max_results: int = 10,
    news_max_results: int = 8,
) -> tuple[str, list[SearchHit], list[SearchHit], str, SearchHit | None]:
    site_query = build_site_query(company_name, city)
    site_candidates = search_web(site_query, max_results=site_max_results)
    selected_site = select_official_site(company_name, city, site_candidates)

    news_query, news_results = collect_company_news(
        company_name=company_name,
        city=city,
        selected_site=selected_site,
        news_max_results=news_max_results,
    )

    return site_query, site_candidates, news_results, news_query, selected_site
