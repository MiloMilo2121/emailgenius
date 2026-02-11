from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone

from .browser import fetch_website_snapshot
from .extraction import infer_company_signals
from .outreach import generate_outreach_email
from .search import discover_company_and_news
from .scoring import evaluate_transition50_eligibility
from .types import AnalysisResult, DiscoveryContext


async def analyze_company(
    *,
    url: str,
    company_name: str,
    headless: bool = True,
) -> AnalysisResult:
    snapshot = await fetch_website_snapshot(url=url, headless=headless)
    signals = infer_company_signals(snapshot.full_text)
    eligibility = evaluate_transition50_eligibility(signals)
    outreach_email = generate_outreach_email(
        company_name=company_name,
        website_url=url,
        signals=signals,
        eligibility=eligibility,
    )

    return AnalysisResult(
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        input_url=url,
        company_name=company_name,
        browser_snapshot=snapshot,
        signals=signals,
        eligibility=eligibility,
        outreach_email=outreach_email,
    )


def analyze_company_sync(*, url: str, company_name: str, headless: bool = True) -> AnalysisResult:
    return asyncio.run(analyze_company(url=url, company_name=company_name, headless=headless))


async def discover_and_analyze_company(
    *,
    company_name: str,
    city: str | None,
    headless: bool = True,
    site_max_results: int = 10,
    news_max_results: int = 8,
) -> AnalysisResult:
    site_query, site_candidates, news_results, news_query, selected_site = discover_company_and_news(
        company_name=company_name,
        city=city,
        site_max_results=site_max_results,
        news_max_results=news_max_results,
    )

    if selected_site is None:
        raise RuntimeError(
            "Unable to discover an official website candidate. "
            "Try passing a direct URL with `emailgenius analyze <url>`."
        )

    result = await analyze_company(
        url=selected_site.url,
        company_name=company_name,
        headless=headless,
    )
    result.discovery = DiscoveryContext(
        site_query=site_query,
        site_candidates=site_candidates,
        selected_site=selected_site,
        news_query=news_query,
        news_results=news_results,
    )
    return result


def discover_and_analyze_company_sync(
    *,
    company_name: str,
    city: str | None,
    headless: bool = True,
    site_max_results: int = 10,
    news_max_results: int = 8,
) -> AnalysisResult:
    return asyncio.run(
        discover_and_analyze_company(
            company_name=company_name,
            city=city,
            headless=headless,
            site_max_results=site_max_results,
            news_max_results=news_max_results,
        )
    )


def result_to_dict(result: AnalysisResult) -> dict:
    return asdict(result)
