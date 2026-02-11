from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone

from .browser import fetch_website_snapshot
from .extraction import infer_company_signals
from .outreach import generate_outreach_email
from .scoring import evaluate_transition50_eligibility
from .types import AnalysisResult


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


def result_to_dict(result: AnalysisResult) -> dict:
    return asdict(result)
