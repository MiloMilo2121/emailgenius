from __future__ import annotations

import base64
import html as html_lib
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from .types import SearchHit

DUCKDUCKGO_HTML_URL = "https://duckduckgo.com/html/"
BING_SEARCH_URL = "https://www.bing.com/search"
BING_NEWS_SEARCH_URL = "https://www.bing.com/news/search"
DEFAULT_TIMEOUT_S = 15

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


class _DuckDuckGoResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_result_anchor = False
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []
        self._hits: list[SearchHit] = []

    @property
    def hits(self) -> list[SearchHit]:
        return self._hits

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return

        attr_map = {key: (value or "") for key, value in attrs}
        css_class = attr_map.get("class", "")
        href = attr_map.get("href", "")
        if "result__a" not in css_class or not href:
            return

        self._in_result_anchor = True
        self._current_href = href
        self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_result_anchor:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._in_result_anchor:
            return

        title = " ".join("".join(self._current_text_parts).split())
        url = _resolve_ddg_url(self._current_href or "")

        if title and url:
            self._hits.append(SearchHit(title=title, url=url))

        self._in_result_anchor = False
        self._current_href = None
        self._current_text_parts = []


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


def _resolve_ddg_url(raw_href: str) -> str:
    if not raw_href:
        return ""

    href = raw_href.strip()
    if href.startswith("//"):
        href = f"https:{href}"

    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)

    return href if parsed.scheme in {"http", "https"} else ""


def parse_duckduckgo_html(html: str, *, max_results: int = 8) -> list[SearchHit]:
    parser = _DuckDuckGoResultParser()
    parser.feed(html)

    seen: set[str] = set()
    hits: list[SearchHit] = []

    for hit in parser.hits:
        url = hit.url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        hits.append(hit)
        if len(hits) >= max_results:
            break

    return hits


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


def _decode_bing_redirect(raw_href: str) -> str:
    href = html_lib.unescape(raw_href.strip())
    if href.startswith("//"):
        href = f"https:{href}"

    parsed = urlparse(href)
    if not parsed.netloc.endswith("bing.com") or not parsed.path.startswith("/ck/a"):
        return href if parsed.scheme in {"http", "https"} else ""

    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded:
        return href

    # Bing often prefixes base64 payload with "a1".
    if encoded.startswith("a1"):
        encoded = encoded[2:]

    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8", errors="ignore")
    except Exception:
        return href

    return decoded if decoded.startswith(("http://", "https://")) else href


def parse_bing_html(html: str, *, max_results: int = 8) -> list[SearchHit]:
    pattern = re.compile(
        r"<h2[^>]*>\s*<a[^>]*href=\"(?P<href>[^\"]+)\"[^>]*>(?P<title>.*?)</a>\s*</h2>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for match in pattern.finditer(html):
        url = _decode_bing_redirect(match.group("href"))
        title_html = match.group("title")
        title = " ".join(_strip_tags(html_lib.unescape(title_html)).split())
        if not title or not url or url in seen:
            continue

        seen.add(url)
        hits.append(SearchHit(title=title, url=url))
        if len(hits) >= max_results:
            break

    return hits


def parse_bing_news_html(html: str, *, max_results: int = 8) -> list[SearchHit]:
    pattern = re.compile(
        r"<a[^>]*class=\"title\"[^>]*href=\"(?P<href>[^\"]+)\"[^>]*>(?P<title>.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for match in pattern.finditer(html):
        url = html_lib.unescape(match.group("href")).strip()
        title_html = match.group("title")
        title = " ".join(_strip_tags(html_lib.unescape(title_html)).split())
        if not title or not url or url in seen:
            continue
        if not url.startswith(("http://", "https://")):
            continue

        seen.add(url)
        hits.append(SearchHit(title=title, url=url))
        if len(hits) >= max_results:
            break

    return hits


def _search_bing(query: str, *, max_results: int, timeout_s: int) -> list[SearchHit]:
    url = f"{BING_SEARCH_URL}?{urlencode({'q': query, 'setlang': 'it'})}"
    header_candidates: tuple[dict[str, str], ...] = (
        {"User-Agent": "Mozilla/5.0"},
        {},
    )

    for headers in header_candidates:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout_s) as response:
                html = response.read().decode("utf-8", errors="ignore")
        except Exception:
            continue

        hits = parse_bing_html(html, max_results=max_results)
        if hits:
            return hits

    return []


def search_news_web(query: str, *, max_results: int = 8, timeout_s: int = DEFAULT_TIMEOUT_S) -> list[SearchHit]:
    url = f"{BING_NEWS_SEARCH_URL}?{urlencode({'q': query, 'setlang': 'it'})}"
    header_candidates: tuple[dict[str, str], ...] = (
        {"User-Agent": "Mozilla/5.0"},
        {},
    )

    for headers in header_candidates:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout_s) as response:
                html = response.read().decode("utf-8", errors="ignore")
        except Exception:
            continue

        hits = parse_bing_news_html(html, max_results=max_results)
        if hits:
            return hits

    # Fallback to generic web search when the news vertical fails.
    return search_web(query, max_results=max_results, timeout_s=timeout_s)


def search_web(query: str, *, max_results: int = 8, timeout_s: int = DEFAULT_TIMEOUT_S) -> list[SearchHit]:
    # Bing first: more reliable in this runtime than DDG HTML endpoint.
    bing_hits = _search_bing(query, max_results=max_results, timeout_s=timeout_s)
    if bing_hits:
        return bing_hits

    payload = urlencode({"q": query, "kl": "it-it"}).encode("utf-8")
    request = Request(
        DUCKDUCKGO_HTML_URL,
        data=payload,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_s) as response:
            html = response.read().decode("utf-8", errors="ignore")
        ddg_hits = parse_duckduckgo_html(html, max_results=max_results)
    except Exception:
        ddg_hits = []

    if ddg_hits:
        return ddg_hits

    return _search_bing(query, max_results=max_results, timeout_s=timeout_s)


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
