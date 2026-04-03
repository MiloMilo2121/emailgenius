from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .types import LeadCompany, LeadContact, ResearchSource

_EXA_SEARCH_URL = "https://api.exa.ai/search"


class ExaClient:
    def __init__(self, api_key: str | None) -> None:
        self._api_key = (api_key or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def collect_company_research(
        self,
        *,
        company: LeadCompany,
        contact: LeadContact | None,
        max_official_results: int = 3,
        max_news_results: int = 4,
    ) -> dict[str, object]:
        domain = _domain_from_url(company.website)
        company_query = company.company_name
        if domain:
            company_query = f"{company.company_name} {domain}"

        company_lookup = self.search(
            query=company_query,
            category="company",
            num_results=1,
        )
        official_pages = self.search(
            query=f"{company.company_name} company overview services news",
            include_domains=[domain] if domain else None,
            num_results=max_official_results,
        )
        news_query = f"{company.company_name} recent news expansion investment partnership"
        if contact and contact.title:
            news_query = f"{news_query} {contact.title}"
        news_results = self.search(
            query=news_query,
            category="news",
            num_results=max_news_results,
            start_published_date=(date.today() - timedelta(days=210)).isoformat(),
        )

        return {
            "company_name": company.company_name,
            "domain": domain,
            "company_lookup": company_lookup,
            "official_pages": official_pages,
            "news_results": news_results,
            "contact": {
                "full_name": contact.full_name if contact else "",
                "title": contact.title if contact else "",
            },
        }

    def search(
        self,
        *,
        query: str,
        category: str | None = None,
        include_domains: list[str] | None = None,
        num_results: int = 5,
        start_published_date: str | None = None,
    ) -> dict[str, object]:
        if not self.configured:
            return {}

        payload: dict[str, object] = {
            "query": query,
            "type": "auto",
            "numResults": max(1, int(num_results)),
        }
        if category:
            payload["category"] = category
        if include_domains:
            cleaned_domains = [item for item in include_domains if item]
            if cleaned_domains:
                payload["includeDomains"] = cleaned_domains
        if start_published_date:
            payload["startPublishedDate"] = start_published_date

        response = self._post_json(_EXA_SEARCH_URL, payload)
        if not isinstance(response, dict):
            return {}

        results = response.get("results")
        entities = response.get("entities")
        parsed_results = _coerce_results(results)
        parsed_entities = entities if isinstance(entities, list) else []
        return {
            "query": query,
            "results": [asdict(item) for item in parsed_results],
            "entities": parsed_entities,
        }

    def _post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._api_key,
                "User-Agent": "EmailGenius/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=25) as response:
                raw = response.read().decode("utf-8", errors="ignore")
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"Exa request failed: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Exa returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Exa returned unexpected payload")
        return parsed


def _coerce_results(value: object) -> list[ResearchSource]:
    if not isinstance(value, list):
        return []

    out: list[ResearchSource] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        out.append(
            ResearchSource(
                title=title,
                url=url,
                snippet=_excerpt_from_result(item),
                source_type=str(item.get("type") or item.get("subdomain") or "").strip(),
                published_at=str(item.get("publishedDate") or item.get("published_at") or "").strip() or None,
            )
        )
    return out


def _excerpt_from_result(item: dict[str, Any]) -> str:
    for key in ("text", "snippet", "summary", "highlights"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:700]
        if isinstance(value, list):
            parts = [str(part).strip() for part in value if str(part).strip()]
            if parts:
                return " ".join(parts)[:700]
    highlight_scores = item.get("highlightScores")
    if isinstance(highlight_scores, list):
        parts = [str(part).strip() for part in highlight_scores if str(part).strip()]
        if parts:
            return " ".join(parts)[:700]
    return ""


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host or None
