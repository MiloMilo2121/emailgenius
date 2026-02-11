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


def _filter_news_results(news_results: list[SearchHit], selected_site: SearchHit | None) -> list[SearchHit]:
    if not news_results:
        return []

    selected_domain = _domain(selected_site.url) if selected_site else ""
    filtered: list[SearchHit] = []
    seen_urls: set[str] = set()

    for hit in news_results:
        host = _domain(hit.url)
        if selected_domain and (host == selected_domain or host.endswith(f".{selected_domain}")):
            continue
        if host in BLOCKED_OFFICIAL_SITE_DOMAINS:
            continue
        if hit.url in seen_urls:
            continue
        seen_urls.add(hit.url)
        filtered.append(hit)

    return filtered or news_results


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

    news_query = build_news_query(company_name, city)
    if selected_site is not None:
        news_query = f"{news_query} -site:{_domain(selected_site.url)}"
    news_raw_results = search_news_web(news_query, max_results=max(news_max_results * 2, news_max_results))
    news_results = _filter_news_results(news_raw_results, selected_site)[:news_max_results]

    return site_query, site_candidates, news_results, news_query, selected_site
